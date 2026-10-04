"""以圖搜圖：觀眾實拍照評估（Q-2026-10-04-06 第 1、2 項，docs/adr/002）。
在程序內執行，不用開後端、不用索引（要有 Chinese-CLIP）；結果存 eval/runs/<run_id>-met-photos.json。

ADR 002 的門檻（CLIP ≥ image_threshold、ORB inlier ≥ verify_min_inliers）是用同一張數位原圖加工的
模擬照、和 Commons 上正面清楚的展場照校正的。這支改用 The Met Dataset 的觀眾實拍照
（eval/met_photo_set.json，由 make_met_photo_set.py 產生）量兩件事：

1. 真實手機照的校正：
   - 收錄：照片拍到的畫在知識庫裡，要認得出來；
   - 未收錄：同一批畫作照片，把它拍到的那幅從知識庫拿掉（leave-one-out），
     要回「知識庫中沒有這幅畫」；
     另外 900 多張器物、雕塑的照片（拍到的館藏不在評估用知識庫裡）也都要回「沒有」；
   - 掃 image_threshold × verify_min_inliers，看兩邊怎麼取捨。
2. 知識庫變大時第一階段（Chinese-CLIP）找不找得到：知識庫只有 3 幅時 verify_top_n: 3 等於每幅都驗；
   這裡模擬 3 幅到 2,400 多幅的知識庫，量正解有沒有排進前 N 名（recall@N），以及整套流程的正確率、
   把 verify_top_n 調大能救回多少、要多花多少時間。

評估用的知識庫（不進 kb/，只在記憶體裡）：
- 照片拍到的 166 幅畫：Met 開放 API 的 primaryImage 縮到長邊 1024（和 kb/images 一樣）；
- 干擾項：make_met_set.py 抽的大都會畫作（eval/met_set.json、met_train.json，扣掉上面重複的），
  用 data/met_eval/ 已經抓好的 primaryImageSmall（長邊約 600）；
- kb、kb_staging 的 5 幅畫。

照片：Met Dataset 附的照片長邊只有 500 px（500px）；Flickr 來的另外抓原圖
（原圖，經 load_image 縮到 1024），兩種都跑——觀眾上傳的照片會是後者。
和 search_service.identify 同一套：照片經 load_image、知識庫圖的 CLIP 向量照建索引的算法、
ORB 用 verify.features／verify.match（含退化 homography 的檢查，geometry＝retrieval）。

圖不 commit：館藏圖存 data/met_photo/exhibits/，Flickr 原圖存 data/met_photo/flickr/，
CLIP 向量快取在 data/met_photo/vecs-<版本>.npz。
第一次跑要抓約 166 張館藏圖（每張 1–3 MB）與 160 張 Flickr 原圖；
Flickr 限流時這次先用 500px，下次再跑補抓。
全部跑完約 1 小時（CPU）：CLIP 向量第一次約 15 分鐘，ORB 比對約 50 分鐘。
用法：python eval/run_met_photo_eval.py [--seeds 20]
"""

import argparse
import functools
import json
import random
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "eval"))

from make_met_photo_set import fetch_dataset  # noqa: E402
from make_met_set import DATA as MET_EVAL  # noqa: E402
from make_met_set import Met  # noqa: E402

from app.core.config import get_models_config  # noqa: E402
from app.rag import verify  # noqa: E402
from app.rag.embedders import embed_image  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
SET = json.loads((ROOT / "eval" / "met_photo_set.json").read_text(encoding="utf-8"))
DS = ROOT / "data" / "met_dataset"
DATA = ROOT / "data" / "met_photo"
UA = {"User-Agent": "art-rag-eval/0.1 (school project; https://github.com/Sinediesoft/art-rag)"}
FLICKR_INTERVAL_S = 1.0
CAND = 20  # 每張照片對 CLIP 前 CAND 名都算 inlier（掃 verify_top_n 用）
TOP_NS = (1, 3, 5, 10, 20)
SIZES = (3, 10, 30, 100, 300, 1000)  # 加上「全部」
E2E_SIZES = (3, 30, 300)  # 整套流程依知識庫大小（ORB 比較花時間，只跑幾個點）
INLIER_GRID = (8, 10, 12, 15, 20, 25, 30, 40)
CLIP_GRID = (0.5, 0.6, 0.65, 0.7)


# ---- 抓圖 ----


def fetch_exhibits(objs: dict[str, dict]) -> None:
    out = DATA / "exhibits"
    out.mkdir(parents=True, exist_ok=True)
    todo = [k for k, o in objs.items() if o["image_url"] and not (out / f"{k}.jpg").exists()]
    if not todo:
        return
    print(f"從 Met 開放 API 抓 {len(todo)} 張館藏圖到 {out.relative_to(ROOT)}/（縮到長邊 1024）")
    met = Met()
    for i, k in enumerate(todo, 1):
        r = met._get(objs[k]["image_url"])
        if r is None:
            print(f"  {k} 抓不到")
            continue
        load_image(r.content).save(out / f"{k}.jpg", quality=92)
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}")


def fetch_flickr(queries: list[dict]) -> None:
    out = DATA / "flickr"
    out.mkdir(parents=True, exist_ok=True)
    todo = [q for q in queries if q["flickr_url"] and not flickr_path(q).exists()]
    if not todo:
        return
    print(f"從 Flickr 抓 {len(todo)} 張原圖到 {out.relative_to(ROOT)}/（縮到長邊 1024）")
    # 用 curl 抓：2026-10-04 同一個網址 httpx 拿到 502／429（換 User-Agent 也一樣），curl 是 200；
    # 連抓一百多張後 Flickr 會限流（429）好幾分鐘：遇到就停，這次只用抓到的，下次再跑會補抓
    tmp = out / "_download.part"
    for i, q in enumerate(todo, 1):
        for attempt in range(3):
            time.sleep(FLICKR_INTERVAL_S)
            r = subprocess.run(
                ["curl", "-sS", "-L", "-m", "120", "-A", UA["User-Agent"], "-o", str(tmp)]
                + ["-w", "%{http_code}", q["flickr_url"]],
                capture_output=True,
                text=True,
            )
            code = r.stdout.strip()
            if code in ("404", "410"):
                print(f"  {q['file']} 已不在 Flickr（{code}），只用 500px")
                break
            if code == "429":
                left = len(todo) - i + 1
                print(f"  Flickr 限流（429）：剩下 {left} 張這次只用 500px，之後再跑會補抓")
                tmp.unlink(missing_ok=True)
                return
            if code == "200":
                try:
                    load_image(tmp.read_bytes()).save(flickr_path(q), quality=92)
                    break
                except OSError as e:
                    code = e.__class__.__name__
            wait = 5 * (attempt + 1)
            print(f"  {q['file']} 失敗（{code or r.stderr.strip()}），{wait} 秒後重試")
            time.sleep(wait)
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}")
    tmp.unlink(missing_ok=True)


def flickr_path(q: dict) -> Path:
    return DATA / "flickr" / Path(q["file"]).name


# ---- 向量與特徵點 ----


class _Kp:
    """verify.match 只用到 keypoint 的 .pt：存成陣列，幾千張圖的 cv2.KeyPoint 物件太占記憶體。"""

    __slots__ = ("pts",)

    def __init__(self, kps) -> None:
        self.pts = np.float32([k.pt for k in kps]) if len(kps) else np.zeros((0, 2), np.float32)

    def __len__(self) -> int:
        return len(self.pts)

    def __getitem__(self, i: int) -> "_Pt":
        return _Pt(self.pts[i])


class _Pt:
    __slots__ = ("pt",)

    def __init__(self, p) -> None:
        self.pt = (float(p[0]), float(p[1]))


def compact(f):
    kp, desc, s, shape = f
    return _Kp(kp), desc, s, shape


class Vecs:
    """CLIP 向量快取（key＝相對路徑）。"""

    def __init__(self) -> None:
        rev = get_models_config().embeddings["image"].revision[:8]
        self.path = DATA / f"vecs-{rev}.npz"
        self.vecs: dict[str, np.ndarray] = {}
        if self.path.exists():
            z = np.load(self.path)
            self.vecs = dict(zip(z["keys"].tolist(), z["vecs"], strict=True))
        self.dirty = 0

    def get(self, path: Path) -> np.ndarray:
        key = path.relative_to(ROOT).as_posix()
        if key not in self.vecs:
            self.vecs[key] = embed_image(load_image(path))
            self.dirty += 1
            if self.dirty % 200 == 0:
                self.save()
                print(f"  已算 {self.dirty} 個向量")
        return self.vecs[key]

    def save(self) -> None:
        if self.dirty:
            np.savez(
                self.path, keys=np.array(list(self.vecs)), vecs=np.stack(list(self.vecs.values()))
            )


# ---- 評估用的知識庫 ----


def distractors() -> list[dict]:
    """make_met_set.py 抽的大都會畫作（評估集＋訓練集）；畫作卡推測的評估已經抓過圖的話直接用。"""
    return [
        e
        for name in ("met_set.json", "met_train.json")
        for e in json.loads((ROOT / "eval" / name).read_text(encoding="utf-8"))["items"]
        if e["kind"] == "painting"
    ]


def fetch_distractors() -> None:
    todo = [e for e in distractors() if not (MET_EVAL / f"{e['id']}.jpg").exists()]
    if not todo:
        return
    print(f"從 Met 開放 API 抓 {len(todo)} 張干擾項到 {MET_EVAL.relative_to(ROOT)}/")
    MET_EVAL.mkdir(parents=True, exist_ok=True)
    met = Met()
    for i, e in enumerate(todo, 1):
        met.image(e["image_url"], MET_EVAL / f"{e['id']}.jpg")
        if i % 200 == 0:
            print(f"  {i}/{len(todo)}")


def build_index(objs: dict[str, dict]) -> list[tuple[str, Path, str]]:
    """(key, 圖檔, 來源)；同一件館藏只收一次，照片拍到的那幾件用 1024 的圖。"""
    items: dict[str, tuple[Path, str]] = {}
    for d in ("kb", "kb_staging"):
        for p in sorted((ROOT / d / "images").glob("*.jpg")):
            key = f"met:{p.stem[4:]}" if p.stem.startswith("met-") else f"{d}:{p.stem}"
            items[key] = (p, d)
    for k in objs:
        p = DATA / "exhibits" / f"{k}.jpg"
        if p.exists():
            items.setdefault(f"met:{k}", (p, "photo_target"))
    for e in distractors():
        p = MET_EVAL / f"{e['id']}.jpg"
        if p.exists():
            items.setdefault(f"met:{e['id']}", (p, "distractor"))
    return [(k, p, src) for k, (p, src) in items.items()]


# ---- 模擬 identify ----


class Query:
    def __init__(self, q: dict, variant: str, path: Path, ev: "Evaluator", vecs: "Vecs"):
        self.q, self.variant, self.path = q, variant, path
        img = load_image(path)
        self.vec = vecs.get(path)  # ＝embed_image(img)，有快取
        self.feats = compact(verify.features(img))
        self.scores = ev.mat @ self.vec
        self.order = np.argsort(-self.scores)
        own = f"met:{q['met_id']}"
        self.own = ev.keys.index(own) if own in ev.keys else None
        self.inl: dict[int, int] = {}

    def own_rank(self, allowed: np.ndarray | None = None) -> int:
        if allowed is None:
            return int(np.sum(self.scores > self.scores[self.own]))
        return int(np.sum(self.scores[allowed] > self.scores[self.own]))


class Evaluator:
    def __init__(self, index: list[tuple[str, Path, str]], vecs: Vecs) -> None:
        self.keys = [k for k, _, _ in index]
        self.paths = [p for _, p, _ in index]
        self.src = [s for _, _, s in index]
        self.cfg = get_models_config().retrieval
        print(f"評估用知識庫 {len(index)} 幅：算 CLIP 向量")
        self.mat = np.stack([vecs.get(p) for p in self.paths])
        self.kb_feats: dict[int, tuple] = {}
        self.t_match: list[float] = []
        self.t_feat: list[float] = []

    def kb_feat(self, j: int):
        if j not in self.kb_feats:
            t = time.perf_counter()
            self.kb_feats[j] = compact(verify.features(Image.open(self.paths[j])))
            self.t_feat.append(time.perf_counter() - t)
        return self.kb_feats[j]

    def inliers(self, qy: Query, j: int) -> int:
        if j not in qy.inl:
            ref = self.kb_feat(j)
            t = time.perf_counter()
            qy.inl[j] = verify.count_inliers(qy.feats, ref, geometry=self.cfg)
            self.t_match.append(time.perf_counter() - t)
        return qy.inl[j]

    def decide(
        self,
        qy: Query,
        threshold: float,
        top_n: int,
        min_inliers: int,
        allowed: np.ndarray | None = None,
    ) -> int | None:
        """和 identify 同一套：CLIP 前 top_n 名、過門檻的做幾何驗證，inlier 最多的通過者勝出。
        allowed：只在這些知識庫項目裡找（模擬較小的知識庫、或拿掉正解）。回傳知識庫索引或 None。"""
        order = qy.order if allowed is None else allowed[np.argsort(-qy.scores[allowed])]
        best, best_inl = None, -1
        for j in order[:top_n]:
            if qy.scores[j] < threshold:
                break  # 由高到低排，後面的也不會過
            n = self.inliers(qy, int(j))
            if n >= min_inliers and n > best_inl:
                best, best_inl = int(j), n
        return best


def stats(values: list[float]) -> dict:
    if not values:
        return {}
    v = sorted(values)
    q = lambda p: v[min(len(v) - 1, int(p * len(v)))]  # noqa: E731
    return {
        "n": len(v),
        "min": round(v[0], 4),
        "p10": round(q(0.1), 4),
        "median": round(statistics.median(v), 4),
        "p90": round(q(0.9), 4),
        "max": round(v[-1], 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20, help="每個知識庫大小抽幾次（recall）")
    ap.add_argument("--limit", type=int, help="收錄、未收錄的照片各只跑前 N 張（試跑用）")
    args = ap.parse_args()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    cfg = get_models_config().retrieval
    cur = {
        "threshold": float(cfg["image_threshold"]),
        "top_n": int(cfg["verify_top_n"]),
        "min_inliers": int(cfg["verify_min_inliers"]),
    }

    paint_objs = {k: o for k, o in SET["objects"].items() if o["kind"] == "painting"}
    DATA.mkdir(parents=True, exist_ok=True)
    fetch_dataset()
    fetch_exhibits(paint_objs)
    fetch_distractors()
    index = build_index(paint_objs)
    # 收錄＝照片拍到的館藏在評估用知識庫裡。除了 166 幅畫，干擾項裡的浮世繪、羅馬壁畫也有觀眾照片
    # （館方分類是 Prints、Miscellaneous-Paintings）；其餘（器物、雕塑…）都是「未收錄」
    indexed = {k for k, _, _ in index}
    pos_q = [q for q in SET["queries"] if f"met:{q['met_id']}" in indexed]
    neg_q = [q for q in SET["queries"] if f"met:{q['met_id']}" not in indexed]
    if args.limit:
        pos_q, neg_q = pos_q[: args.limit], neg_q[: args.limit]
    fetch_flickr(pos_q)

    vecs = Vecs()
    ev = Evaluator(index, vecs)
    vecs.save()
    n_index = len(ev.keys)
    print(
        f"  照片拍到的畫 {ev.src.count('photo_target')}、干擾項 {ev.src.count('distractor')}、"
        f"kb {ev.src.count('kb')}、kb_staging {ev.src.count('kb_staging')}"
    )

    # 照片：收錄的兩種解析度；未收錄的（器物、雕塑…）只有 500px
    queries: list[Query] = []
    cases = [(q, "500px", DS / q["file"]) for q in pos_q]
    cases += [(q, "原圖", flickr_path(q)) for q in pos_q if flickr_path(q).exists()]
    cases += [(q, "500px", DS / q["file"]) for q in neg_q]
    print(
        f"照片 {len(cases)} 張（收錄 {len(pos_q)}、未收錄 {len(neg_q)}）："
        f"算向量、特徵點、CLIP 前 {CAND} 名的 inlier"
    )
    for i, (q, variant, path) in enumerate(cases, 1):
        qy = Query(q, variant, path, ev, vecs)
        for j in qy.order[:CAND]:
            ev.inliers(qy, int(j))
        if qy.own is not None:
            ev.inliers(qy, qy.own)  # 正解一定算：CLIP 沒排進來時，幾何驗證過不過得了
        queries.append(qy)
        if i % 100 == 0:
            print(f"  {i}/{len(cases)}")
    vecs.save()

    # 研究團隊自己拍的（沒有 Flickr 網址）刻意拍難：大角度斜拍、反光、只拍到畫框一角；
    # Flickr 的是一般觀眾的照片。同一批 Flickr 照片的 500px 和原圖對照，看解析度的影響
    pos = {
        "原圖（Flickr）": [qy for qy in queries if qy.own is not None and qy.variant == "原圖"],
        "500px（Flickr）": [
            qy
            for qy in queries
            if qy.own is not None and qy.variant == "500px" and qy.q["flickr_url"]
        ],
        "500px（研究團隊）": [
            qy
            for qy in queries
            if qy.own is not None and qy.variant == "500px" and not qy.q["flickr_url"]
        ],
    }
    unindexed = [qy for qy in queries if qy.own is None]
    everything = np.arange(n_index)

    def loo(qy: Query) -> np.ndarray:
        return everything[everything != qy.own]

    def score(t: float, n: int, m: int) -> dict:
        out = {}
        for v, group in pos.items():
            hit = [ev.decide(qy, t, n, m) for qy in group]
            out[v] = {
                "n": len(group),
                "correct": sum(h == qy.own for h, qy in zip(hit, group, strict=True)),
                "wrong": sum(
                    h is not None and h != qy.own for h, qy in zip(hit, group, strict=True)
                ),
                "loo_false_accepts": sum(
                    ev.decide(qy, t, n, m, loo(qy)) is not None for qy in group
                ),
            }
        out["not_indexed"] = {
            "n": len(unindexed),
            "false_accepts": sum(ev.decide(qy, t, n, m) is not None for qy in unindexed),
        }
        return out

    # ---- 1. 目前的設定、分布、門檻掃描 ----
    current = score(cur["threshold"], cur["top_n"], cur["min_inliers"])

    def best_wrong(qy: Query, src: str | None = None) -> tuple[int | None, int]:
        """CLIP 前 CAND 名裡、不是正解的 inlier 最多的那幅：(知識庫索引, inlier)。"""
        js = [int(j) for j in qy.order[:CAND] if j != qy.own and (src is None or ev.src[j] == src)]
        j = max(js, key=lambda j: qy.inl[j], default=None)
        return j, (0 if j is None else qy.inl[j])

    def wrong_inliers(qy: Query, src: str | None = None) -> int:
        return best_wrong(qy, src)[1]

    dist = {}
    for v, group in pos.items():
        dist[v] = {
            "own_clip": stats([float(qy.scores[qy.own]) for qy in group]),
            "own_rank": stats([qy.own_rank() for qy in group]),
            "own_inliers": stats([qy.inl[qy.own] for qy in group]),
            "best_wrong_inliers": stats([wrong_inliers(qy) for qy in group]),
            "best_wrong_inliers_1024": stats([wrong_inliers(qy, "photo_target") for qy in group]),
            "best_wrong_inliers_600": stats([wrong_inliers(qy, "distractor") for qy in group]),
            "best_wrong_clip": stats(
                [max(float(qy.scores[j]) for j in qy.order[:2] if j != qy.own) for qy in group]
            ),
        }
    dist["not_indexed"] = {
        "best_clip": stats([float(qy.scores[qy.order[0]]) for qy in unindexed]),
        "best_inliers": stats([wrong_inliers(qy) for qy in unindexed]),
    }
    # 門檻掃描：CLIP 門檻 × inlier 下限（verify_top_n 照舊）；
    # 另外看 verify_top_n 調大時 inlier 下限要不要跟著調
    sweep = [
        {"threshold": t, "min_inliers": m, "top_n": cur["top_n"]} | score(t, cur["top_n"], m)
        for t in CLIP_GRID
        for m in INLIER_GRID
    ] + [
        {"threshold": cur["threshold"], "min_inliers": m, "top_n": n}
        | score(cur["threshold"], n, m)
        for n in (10, 20)
        for m in INLIER_GRID
    ]

    # ---- 2. 知識庫大小 × 第一階段召回 × verify_top_n ----
    rng = random.Random(0)
    recall = {}
    for v, group in pos.items():
        rows = {}
        for size in (*SIZES, n_index):
            ranks = []
            for qy in group:
                others = everything[everything != qy.own]
                for _ in range(args.seeds if size < n_index else 1):
                    pick = np.array(rng.sample(list(others), min(size - 1, len(others))), int)
                    ranks.append(qy.own_rank(pick))
            rows[str(size)] = {
                f"@{k}": round(sum(r < k for r in ranks) / len(ranks), 4) for k in TOP_NS
            }
        recall[v] = rows

    e2e = {}
    for v, group in pos.items():
        rows = {}
        for size in (*E2E_SIZES, n_index):
            seeds = 3 if size < n_index else 1
            for top_n in (3, 5, 10, 20):
                correct = wrong = fa = total = 0
                for qy in group:
                    others = everything[everything != qy.own]
                    for s in range(seeds):
                        r = random.Random(f"{qy.q['file']}|{size}|{s}")
                        pick = np.array(r.sample(list(others), min(size - 1, len(others))), int)
                        h = ev.decide(
                            qy, cur["threshold"], top_n, cur["min_inliers"], np.append(pick, qy.own)
                        )
                        correct += h == qy.own
                        wrong += h is not None and h != qy.own
                        fa += (
                            ev.decide(qy, cur["threshold"], top_n, cur["min_inliers"], pick)
                            is not None
                        )
                        total += 1
                rows[f"{size}|top{top_n}"] = {
                    "n": total,
                    "correct": round(correct / total, 4),
                    "wrong": round(wrong / total, 4),
                    "loo_false_accept": round(fa / total, 4),
                }
        e2e[v] = rows

    timing = {
        "orb_match_ms": stats([t * 1000 for t in ev.t_match]),
        "kb_features_ms": stats([t * 1000 for t in ev.t_feat]),
    }

    rows = []
    for qy in queries:
        top = int(qy.order[0])
        h = ev.decide(qy, cur["threshold"], cur["top_n"], cur["min_inliers"])
        rows.append(
            {
                "file": qy.q["file"],
                "variant": qy.variant,
                "kind": qy.q["kind"],
                "met_id": qy.q["met_id"],
                "title": SET["objects"].get(str(qy.q["met_id"]), {}).get("title"),
                "photographer": qy.q["photographer"],
                "own_clip": None if qy.own is None else round(float(qy.scores[qy.own]), 4),
                "own_rank": None if qy.own is None else qy.own_rank(),
                "own_inliers": None if qy.own is None else qy.inl[qy.own],
                "top1": ev.keys[top],
                "top1_clip": round(float(qy.scores[top]), 4),
                "top1_inliers": qy.inl[top],
                "best_wrong": (lambda j, n: None if j is None else ev.keys[j])(*best_wrong(qy)),
                "best_wrong_inliers": wrong_inliers(qy),
                "predicted": None if h is None else ev.keys[h],
                "loo_predicted": None
                if qy.own is None
                else (lambda x: None if x is None else ev.keys[x])(
                    ev.decide(qy, cur["threshold"], cur["top_n"], cur["min_inliers"], loo(qy))
                ),
            }
        )

    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "config": cur | {"index_size": n_index, "cand": CAND, "set": SET["source"]},
        "summary": {
            "current": current,
            "distributions": dist,
            "sweep": sweep,
            "recall": recall,
            "end_to_end": e2e,
            "timing": timing,
        },
        "rows": rows,
    }
    path = ROOT / "eval" / "runs" / f"{run_id}-met-photos.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print()
    print(
        f"評估用知識庫 {n_index} 幅；目前設定：CLIP ≥ {cur['threshold']}、前 {cur['top_n']} 名、"
        f"inlier ≥ {cur['min_inliers']}"
    )
    for v in pos:
        c = current[v]
        print(
            f"  收錄的照片（{v}）{c['n']} 張：認對 {c['correct']}、認錯 {c['wrong']}；"
            f"拿掉正解後被認成別幅 {c['loo_false_accepts']}"
        )
        d = dist[v]
        print(
            f"    正解 CLIP {d['own_clip']}\n    正解名次 {d['own_rank']}\n"
            f"    正解 inlier {d['own_inliers']}\n    最多的錯誤 inlier {d['best_wrong_inliers']}"
        )
    o = current["not_indexed"]
    print(
        f"  未收錄的館藏照片（器物、雕塑…）{o['n']} 張：被認成畫 {o['false_accepts']}"
        f"（最多 inlier {dist['not_indexed']['best_inliers']}）"
    )
    print("門檻掃描：verify_top_n / CLIP 門檻 / inlier 下限")
    print("  → 原圖認對、拿掉正解被認錯、未收錄的館藏被認錯")
    for s in sweep:
        r = s["原圖（Flickr）"]
        print(
            f"  前 {s['top_n']:>2} 名 / {s['threshold']:.2f} / {s['min_inliers']:>2} → "
            f"{r['correct']}/{r['n']}、{r['loo_false_accepts']}、{s['not_indexed']['false_accepts']}"
        )
    print("第一階段召回（正解排進前 N 名的比例）：")
    for v, rows_ in recall.items():
        for size, r in rows_.items():
            print(f"  {v} 知識庫 {size:>5} 幅：" + "  ".join(f"{k} {x:.3f}" for k, x in r.items()))
    print("整套流程（依知識庫大小 × verify_top_n）：")
    for v, rows_ in e2e.items():
        for k, r in rows_.items():
            print(
                f"  {v} {k:<12} 認對 {r['correct']:.3f}  認錯 {r['wrong']:.3f}"
                f"  未收錄被認錯 {r['loo_false_accept']:.3f}"
            )
    print(f"時間：ORB 比對一對 {timing['orb_match_ms']} ms")
    print(f"      知識庫圖抽特徵點 {timing['kb_features_ms']} ms")
    print(f"\n已存 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
