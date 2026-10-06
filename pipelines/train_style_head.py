"""畫作卡推測的題材、媒材線性分類頭（docs/adr/018，Q-2026-10-04-04 第 3 項）。

iMet 比賽的主流做法是多標籤：每個標籤各自 sigmoid、各自調門檻（一幅畫可以同時是風景畫和海景畫）。
這裡把辨識已經載入的 Chinese-CLIP 照片向量（512 維）當特徵，
每個標籤訓練一個 L2 正則化的 logistic regression
（每個標籤 512 個權重＋1 個偏差，不是新模型、不多佔記憶體，ADR 006）：
- 訓練資料：eval/met_train.json（eval/make_met_set.py --train，和評估集、知識庫裡的畫都不重疊）；
- 正則化強度與每個標籤的門檻：5 折交叉驗證的 out-of-fold 機率決定。門檻依 F0.5 調——
  比賽用 F2（偏重找全），畫作卡寫錯比漏寫傷信任，偏重準確；
- 和零樣本比（同一套判分，不經過「像畫作」把關）：大都會評估集（eval/met_set.json）、
  eval/style_truth.json 的原圖與模擬照、大都會評估集做成的模擬照（eval/make_synthetic_photos.py 的
  傾斜、反光、裁切、偏暗、模糊，每幅輪流套一種）。

訓練資料裡正例太少的標籤（館方沒有「風俗畫」「抽象畫」這類標籤）訓練不了，報告裡列出來。
輸出：shared/style_head_v1.npz（權重、偏差、門檻、標準化參數、標籤、Chinese-CLIP 版本、
訓練資料 hash）與 eval/runs/<run_id>-style-head.json（交叉驗證與比較結果）。
照片向量快取在 data/met_eval/vecs-*.npz。

用法：python pipelines/train_style_head.py [--no-save]
"""

import argparse
import functools
import hashlib
import io
import json
import random
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "eval")]

import make_synthetic_photos as synth  # noqa: E402
import numpy as np  # noqa: E402
import run_style_eval as rse  # noqa: E402
from make_met_set import Met  # noqa: E402
from PIL import Image  # noqa: E402
from scipy.optimize import minimize  # noqa: E402

from app.analysis import style  # noqa: E402
from app.core.config import get_models_config  # noqa: E402
from app.rag.embedders import embed_image  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度

FIELDS = ("genre", "media")
MIN_POS = 15  # 訓練資料正例少於這個數的標籤不訓練
FOLDS = 5
LAMBDAS = (1e-3, 1e-2, 1e-1, 1.0)  # L2 強度（特徵先標準化）
BETA = 0.5  # 門檻依 F0.5 調：準確的權重是找全的 4 倍
SEED = 0
DATA = ROOT / "data" / "met_eval"
OUT = ROOT / "shared" / "style_head_v1.npz"


# ---- 照片向量（快取） ----


def _bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def synthetic(path: Path, variant: str, seed: int) -> bytes:
    """大都會的棚拍圖做成「模擬實拍照」：和 eval/photos/known 同一套變形，種子固定。"""
    random.seed(seed)
    img = Image.open(path).convert("RGB")
    photo = synth.VARIANTS[variant](img)
    photo.thumbnail((1024, 1024))
    return _bytes(photo)


def embed_all(jobs: dict[str, Callable[[], bytes]]) -> dict[str, np.ndarray]:
    spec = get_models_config().embeddings["image"]
    cache = DATA / f"vecs-{spec.revision[:8]}.npz"
    vecs: dict[str, np.ndarray] = {}
    if cache.exists():
        z = np.load(cache)
        vecs = dict(zip(z["keys"].tolist(), z["vecs"], strict=True))
    todo = [k for k in jobs if k not in vecs]
    for i, k in enumerate(todo, 1):
        vecs[k] = embed_image(load_image(jobs[k]()))
        if i % 200 == 0 or i == len(todo):
            print(f"  照片向量 {i}/{len(todo)}")
            np.savez(cache, keys=np.array(list(vecs)), vecs=np.stack(list(vecs.values())))
    return {k: vecs[k] for k in jobs}


# ---- logistic regression（每個標籤一個） ----


def fit(x: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    n, d = x.shape

    def loss(wb: np.ndarray) -> tuple[float, np.ndarray]:
        w, b = wb[:d], wb[d]
        z = x @ w + b
        p = 1 / (1 + np.exp(-z))
        g = (p - y) / n
        val = float(np.mean(np.logaddexp(0, z) - y * z) + lam * (w @ w) / 2)
        return val, np.append(x.T @ g + lam * w, g.sum())

    r = minimize(loss, np.zeros(d + 1), jac=True, method="L-BFGS-B", options={"maxiter": 1000})
    return r.x[:d], float(r.x[d])


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-z))


def best_threshold(p: np.ndarray, y: np.ndarray) -> tuple[float, dict]:
    """F0.5 最高的門檻（同分取較高的門檻）；怎麼調都沒有對的預測就停用（門檻 > 1）。"""
    best = (0.0, 1.01, {"precision": 0.0, "recall": 0.0})
    for t in np.round(np.arange(0.05, 0.96, 0.01), 2):
        pred = p >= t
        tp = float((pred & (y == 1)).sum())
        if tp == 0:
            continue
        prec, rec = tp / pred.sum(), tp / max(y.sum(), 1)
        f = (1 + BETA**2) * prec * rec / (BETA**2 * prec + rec)
        if f >= best[0]:
            best = (f, float(t), {"precision": round(prec, 3), "recall": round(rec, 3)})
    return best[1], best[2] | {"f05": round(best[0], 3)}


def train_field(x: np.ndarray, truth: list[list[str]], labels: list[str]) -> dict:
    """x：(n, 512) 照片向量；truth：每幅可接受的標籤。回傳權重、門檻與交叉驗證結果。"""
    y_all = np.array([[lab in t for lab in labels] for t in truth], dtype=float)
    pos = y_all.sum(0)
    trainable = [i for i, c in enumerate(pos) if c >= MIN_POS]
    mu, sd = x.mean(0), x.std(0) + 1e-6
    xs = (x - mu) / sd
    folds = np.random.default_rng(SEED).permutation(len(x)) % FOLDS

    # 正則化強度：所有可訓練標籤 out-of-fold log loss 的平均最小者
    oof_by_lam, logloss = {}, {}
    for lam in LAMBDAS:
        oof = np.zeros((len(x), len(labels)))
        for k in range(FOLDS):
            tr, te = folds != k, folds == k
            for i in trainable:
                w, b = fit(xs[tr], y_all[tr, i], lam)
                oof[te, i] = sigmoid(xs[te] @ w + b)
        p = np.clip(oof[:, trainable], 1e-6, 1 - 1e-6)
        y = y_all[:, trainable]
        logloss[lam] = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
        oof_by_lam[lam] = oof
        print(f"    λ={lam:g}：out-of-fold log loss {logloss[lam]:.4f}")
    lam = min(logloss, key=logloss.get)
    oof = oof_by_lam[lam]

    w_all = np.zeros((len(labels), x.shape[1]))
    b_all = np.full(len(labels), -30.0)  # 訓練不了的標籤：機率≈0，永遠不會被選
    thr = np.full(len(labels), 1.01)
    per_label = {}
    for i, lab in enumerate(labels):
        info: dict = {"train_pos": int(pos[i])}
        if i in trainable:
            w_all[i], b_all[i] = fit(xs, y_all[:, i], lam)
            thr[i], cv = best_threshold(oof[:, i], y_all[:, i])
            info |= {"threshold": thr[i], "cv": cv}
        else:
            info["skipped"] = f"正例少於 {MIN_POS}"
        per_label[lab] = info
    head = {"labels": labels, "W": w_all, "b": b_all, "thresholds": thr, "mu": mu, "sd": sd}
    cv_pred = [predict(head, v, raw=p) for v, p in zip(x, oof, strict=True)]
    return head | {
        "lambda": lam,
        "logloss": logloss,
        "per_label": per_label,
        "cv": score(cv_pred, truth),
    }


def predict(head: dict, vec: np.ndarray, raw: np.ndarray | None = None) -> dict:
    """選法直接用後端的 style.head_pick：過門檻的標籤都列出（多標籤），
    沒有任何標籤過門檻就標「看不太出來」，名稱取機率最高的那一個。"""
    p = (
        raw
        if raw is not None
        else sigmoid(((vec - head["mu"]) / head["sd"]) @ head["W"].T + head["b"])
    )
    top, above = style.head_pick(p, head["thresholds"])
    return {
        "name": head["labels"][top],
        "prob": float(p[top]),
        "uncertain": not above,
        "shown": [head["labels"][i] for i in above],
    }


def zero_shot(spec, key: str, vec: np.ndarray) -> dict:
    """目前的零樣本（不經過把關）：只顯示第一名，低於 min_confidence 標「看不太出來」。"""
    names, mat, groups, periods = style._matrices(spec)["tasks"][key]
    f = style._field(key, names, mat, groups, periods, vec, spec)
    return {
        "name": f["name"],
        "prob": f["prob"],
        "uncertain": f["uncertain"],
        "shown": [] if f["uncertain"] else [f["name"]],
    }


def score(preds: list[dict], truth: list[list[str]]) -> dict:
    """第一名對不對；顯示出來的標籤（多標籤）的準確、找全、F0.5；錯了卻沒標「看不太出來」。"""
    n = len(preds)
    ok = [p["name"] in t for p, t in zip(preds, truth, strict=True)]
    kept = [not p["uncertain"] for p in preds]
    shown = sum(len(p["shown"]) for p in preds)
    hit = sum(len(set(p["shown"]) & set(t)) for p, t in zip(preds, truth, strict=True))
    want = sum(len(t) for t in truth)
    prec, rec = hit / max(shown, 1), hit / max(want, 1)
    f05 = (1 + BETA**2) * prec * rec / max(BETA**2 * prec + rec, 1e-9)
    return {
        "n": n,
        "top1": f"{sum(ok)}/{n}",
        "top1_rate": round(sum(ok) / max(n, 1), 3),
        "kept": sum(kept),
        "kept_ok": sum(o and k for o, k in zip(ok, kept, strict=True)),
        "wrong_not_flagged": sum(not o and k for o, k in zip(ok, kept, strict=True)),
        "multi_shown": shown,
        "multi_precision": round(prec, 3),
        "multi_recall": round(rec, 3),
        "multi_f05": round(f05, 3),
        "multi_label_items": sum(len(p["shown"]) > 1 for p in preds),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true", help="只比較，不寫 shared/style_head_v1.npz")
    args = ap.parse_args()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    cfg = get_models_config()
    spec = cfg.style_guess
    train_raw = (ROOT / "eval" / "met_train.json").read_bytes()
    train = [e for e in json.loads(train_raw)["items"] if e["kind"] == "painting"]
    evals = [
        e
        for e in json.loads((ROOT / "eval" / "met_set.json").read_text(encoding="utf-8"))["items"]
        if e["kind"] == "painting"
    ]
    assert not {e["id"] for e in train} & {e["id"] for e in evals}, "訓練集和評估集重疊"
    own = [(n, p, k) for n, p, k in rse.painting_cases()]

    def met_path(e: dict) -> Path:
        return DATA / f"{e['id']}.jpg"

    missing = [e for e in train + evals if not met_path(e).exists()]
    if missing:  # 圖不 commit：別台電腦第一次跑時從 Met 開放 API 補抓
        print(f"補抓 {len(missing)} 張圖到 {DATA.relative_to(ROOT)}/")
        met = Met()
        for e in missing:
            if not met.image(e["image_url"], met_path(e)):
                raise SystemExit(f"抓不到 {e['id']} 的圖（{e['image_url']}）")

    variants = list(synth.VARIANTS)
    jobs: dict[str, Callable[[], bytes]] = {}
    for e in train + evals:
        jobs[f"met:{e['id']}"] = functools.partial(met_path(e).read_bytes)
    for i, e in enumerate(evals):
        v = variants[i % len(variants)]
        jobs[f"synth:{e['id']}:{v}"] = functools.partial(synthetic, met_path(e), v, e["id"])
    for n, p, _ in own:
        jobs[f"own:{n}"] = functools.partial(p.read_bytes)
    print(
        f"訓練 {len(train)} 幅、大都會評估 {len(evals)} 幅（另做同樣多張模擬照）、"
        f"自己的 {len(own)} 張"
    )
    vec = embed_all(jobs)

    report: dict = {"run_id": run_id, "created_at": datetime.now(UTC).isoformat(), "fields": {}}
    heads = {}
    for key in FIELDS:
        labels = list(getattr(spec, key).labels)
        rows = [e for e in train if key in e["truth"]]
        x = np.stack([vec[f"met:{e['id']}"] for e in rows])
        print(f"\n[{key}] 訓練 {len(rows)} 幅、{len(labels)} 個標籤")
        head = train_field(x, [e["truth"][key] for e in rows], labels)
        heads[key] = head

        sets = {
            "met_eval": [
                (vec[f"met:{e['id']}"], e["truth"][key]) for e in evals if key in e["truth"]
            ],
            "met_synth": [
                (vec[f"synth:{e['id']}:{variants[i % len(variants)]}"], e["truth"][key])
                for i, e in enumerate(evals)
                if key in e["truth"]
            ],
            "own_orig": [(vec[f"own:{n}"], rse.TRUTH[k][key]) for n, _, k in own if "__" not in n],
            "own_photo": [(vec[f"own:{n}"], rse.TRUTH[k][key]) for n, _, k in own if "__" in n],
        }
        cmp = {}
        for name, data in sets.items():
            truth = [t for _, t in data]
            cmp[name] = {
                "zero_shot": score([zero_shot(spec, key, v) for v, _ in data], truth),
                "head": score([predict(head, v) for v, _ in data], truth),
            }
        report["fields"][key] = {
            "train_n": len(rows),
            "lambda": head["lambda"],
            "logloss": head["logloss"],
            "cv": head["cv"],
            "per_label": head["per_label"],
            "compare": cmp,
        }
        print(
            f"  λ={head['lambda']:g}；交叉驗證 第一名 {head['cv']['top1']}，"
            f"多標籤 F0.5 {head['cv']['multi_f05']}"
        )
        skipped = [lab for lab, i in head["per_label"].items() if "skipped" in i]
        if skipped:
            print(f"  訓練不了（正例 < {MIN_POS}）：{'、'.join(skipped)}")
        for lab, i in head["per_label"].items():
            if "cv" in i:
                print(
                    f"    {lab:<6} 正例 {i['train_pos']:>4} 門檻 {i['threshold']:.2f}"
                    f" 準確 {i['cv']['precision']:.2f} 找全 {i['cv']['recall']:.2f}"
                )
        print(
            "  比較（第一名｜留下的對幾筆／留下幾筆｜錯了沒標｜"
            "多標籤 準確／找全／F0.5、列了 2 個以上的幅數）："
        )
        for name, c in cmp.items():
            for m in ("zero_shot", "head"):
                s = c[m]
                print(
                    f"    {name:<10} {'零樣本' if m == 'zero_shot' else '分類頭':<4} {s['top1']:>8}"
                    f" | {s['kept_ok']}/{s['kept']} | 錯了沒標 {s['wrong_not_flagged']}"
                    f" | {s['multi_precision']:.2f}／{s['multi_recall']:.2f}／{s['multi_f05']:.2f}"
                    f"、{s['multi_label_items']}"
                )

    clip = cfg.embeddings["image"]
    meta = {
        "version": "style-head-v1",
        "clip": f"{clip.name}@{clip.revision}",
        "train_sha1": hashlib.sha1(train_raw).hexdigest(),
        "train_n": len(train),
        "beta": BETA,
        "run_id": run_id,
    }
    report["meta"] = meta
    path = ROOT / "eval" / "runs" / f"{run_id}-style-head.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {path.relative_to(ROOT)}")
    if not args.no_save:
        arrays = {}
        for key, h in heads.items():
            for a in ("W", "b", "thresholds", "mu", "sd"):
                arrays[f"{key}_{a}"] = np.asarray(h[a], dtype=np.float32)
            arrays[f"{key}_labels"] = np.array(h["labels"])
        np.savez_compressed(OUT, meta=np.array(json.dumps(meta, ensure_ascii=False)), **arrays)
        print(f"→ {OUT.relative_to(ROOT)}（{OUT.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
