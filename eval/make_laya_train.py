"""產生 Laya 的微調資料，第 2、4、6 段放在同一份（混在一起訓練，才不會把前面的題目忘掉）：
- 第 2 段意圖分類（Q-2026-10-14-03）：eval/laya_train/guard_intent.jsonl 的句子（人逐句檢查過的
  id／label／kind／text，附照片的多一個 photo）
  → {"state": …, "questions": …, "gold": {"intent_guard": label}}
- 第 4 段段落驗證、第 6 段生成閘門：eval/laya_train/post_passages.jsonl 的題目，照
  eval/laya_post.py 走真的檢索與 guard.process_passages() 產生請求和標準答案

送進 Laya 的內容要和 art-rag 實際送的一字不差，所以照後端同一套程式做：
mask_pii()（個資遮蔽）→ EntityIndex.find()／pseudonymize()（名稱換代號，
〈連接法蘭〉→ [圖紙A]，主管看得到全部圖紙）→ guard._input_request()（題目文字讀
shared/agent.yaml 的 guard.intent_question、intent_classes）；第 4、6 段同理（laya_post.py）。
改了 agent.yaml 的題目文字、知識庫或檢索規則就要重跑這支、重新訓練。

名稱清單讀 data/index 的檔案索引（make index 會寫），不讀 PostgreSQL：
資料庫裡的索引舊了也照樣能產生。

考卷不能當訓練資料：和 eval/ 底下任何一題（guard_*.jsonl、route_qa.jsonl、qa*.jsonl…的 question）
字元雙字組 Jaccard 相似度 ≥ --max-sim 的句子（第 4、6 段比題目）不放進輸出，並列出來。

用法：uv run --project backend python eval/make_laya_train.py [--out data/laya/laya_train.jsonl]
                                                       [--max-sim 0.5]
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import laya_post  # noqa: E402

from app.agent import entities, guard  # noqa: E402

EVAL = ROOT / "eval"
SOURCE = EVAL / "laya_train" / "guard_intent.jsonl"
POST_SOURCE = EVAL / "laya_train" / "post_passages.jsonl"
NON_WORD = re.compile(r"[\W_]+")


def _norm(text: str) -> str:
    return NON_WORD.sub("", unicodedata.normalize("NFKC", text).lower())


def bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i : i + 2] for i in range(len(t) - 1)} or {t}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def exam_questions() -> list[tuple[str, str]]:
    """eval/ 底下所有題目檔的 question（laya_train/ 的訓練資料不算）。"""
    out = []
    for p in sorted(EVAL.glob("*.jsonl")):
        for line in p.read_text("utf-8").splitlines():
            if line.strip() and (q := json.loads(line).get("question")):
                out.append((p.name, q))
    return out


def load_index() -> entities.EntityIndex:
    laya_post.load_store()
    return entities.get_index()


def too_close(text: str, exams: list, max_sim: float, label: str) -> str | None:
    """和考卷太像就回傳要列出的說明。"""
    grams = bigrams(text)
    sim, name, q = max(((jaccard(grams, g), n, q) for n, q, g in exams), default=(0, "", ""))
    return f"  {label} {text}\n     ≈ {name}：{q}（{sim:.2f}）" if sim >= max_sim else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "data" / "laya" / "laya_train.jsonl"))
    parser.add_argument("--max-sim", type=float, default=0.5, help="和考卷的相似度上限")
    args = parser.parse_args()

    rows = [json.loads(x) for x in SOURCE.read_text("utf-8").splitlines() if x.strip()]
    classes = set(guard._cfg()["intent_classes"])
    ids = Counter(r["id"] for r in rows)
    texts = Counter(r["text"] for r in rows)
    problems = [
        f"{r['id']}：label「{r['label']}」不是 {sorted(classes)}"
        for r in rows
        if r["label"] not in classes
    ]
    problems += [f"id 重複：{k}" for k, n in ids.items() if n > 1]
    problems += [f"句子重複：{k}" for k, n in texts.items() if n > 1]
    problems += [
        f"{r['id']}：photo「{r['photo']}」不是 art／drawing／unknown"
        for r in rows
        if r.get("photo") not in (None, "art", "drawing", "unknown")
    ]
    if problems:
        print("\n".join(problems))
        return 1

    exams = [(name, q, bigrams(q)) for name, q in exam_questions()]
    index = load_index()
    out, dropped, coded = [], [], 0
    for r in rows:
        if why := too_close(r["text"], exams, args.max_sim, r["id"]):
            dropped.append(why)
            continue
        text = guard.mask_pii(r["text"].strip())[0]
        pseudo = index.pseudonymize(text, index.find(text)).text
        coded += pseudo != r["text"]
        state, questions = guard._input_request(pseudo, r.get("photo"))
        out.append(
            {
                "id": r["id"],
                "state": state,
                "questions": questions,
                "gold": {"intent_guard": r["label"]},
            }
        )

    kept = {x["id"] for x in out}

    # 第 4、6 段
    post = laya_post.load_items(POST_SOURCE)
    if problems := laya_post.check_items(post, laya_post.load_store()):
        print("\n".join(problems))
        return 1
    post_kept = []
    for it in post:
        if why := too_close(it["question"], exams, args.max_sim, it["id"]):
            dropped.append(why)
        else:
            post_kept.append(it)
    post_rows = laya_post.build(post_kept)
    out += [{k: r[k] for k in ("id", "state", "questions", "gold")} for r in post_rows]

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), encoding="utf-8"
    )
    print(f"輸出 {len(out)} 筆（{path}）")
    print(f"  第 2 段：來源 {len(rows)} 句 → {len(kept)} 筆")
    print(
        "  各類："
        + "、".join(
            f"{k} {v}" for k, v in Counter(r["label"] for r in rows if r["id"] in kept).items()
        )
    )
    print(f"  代號化或遮蔽個資的句子：{coded}；附照片：{sum('photo' in r for r in rows)}")
    s4 = [r for r in post_rows if r["stage"] == 4]
    s6 = [r for r in post_rows if r["stage"] == 6]
    g4 = Counter((k.split("_")[1], v) for r in s4 for k, v in r["gold"].items())
    g6 = Counter((k, v) for r in s6 for k, v in r["gold"].items())
    print(f"  第 4、6 段：來源 {len(post)} 題 → 第 4 段 {len(s4)} 筆、第 6 段 {len(s6)} 筆")
    print(
        f"    第 4 段：相關 {g4['relevant', True]}／不相關 {g4['relevant', False]}，"
        f"有洩密風險 {g4['leak', True]}／沒有 {g4['leak', False]}"
    )
    print(
        f"    第 6 段：能回答 {g6['answerable', True]}／不能 {g6['answerable', False]}，"
        f"合規 {g6['compliant', True]}／不合規 {g6['compliant', False]}"
    )
    if dropped:
        print(f"和考卷太像、沒放進去的 {len(dropped)} 句（相似度 ≥ {args.max_sim}）：")
        print("\n".join(dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
