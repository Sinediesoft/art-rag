"""產生 LoRA 不檢索對照組的訓練資料（make lora-data，docs/adr/043）。

只用畫作領域 kb/artworks：作品卡（JSON 的基本欄位）、descriptions、regions。
工廠圖紙（kb/parts 等）不進訓練資料：權重裡的知識沒有文件層級，第 3 段 Metadata Filter 擋不住。
「（未審核）」的段落也不用（例如〈有絲柏的麥田〉的觀眾留言，內容是要助理洩漏內部資料的指令）。

三種題目（比例約 75／15／10）：
- fact：每段 20–30 種問法（外洩過濾後計數，不到 20 就失敗），由地端模型依段落產生問與答，
  答案要能在段落裡找到依據
- refusal：問知識庫沒有記載的事，固定回「知識庫中沒有這方面的資料。」
- retain：一般藝術常識，答案由底模自己產生（保留原本的能力）；避開遺忘評估 qa_general 的題材

每筆訓練樣本都呼叫 prompt.build_messages(..., use_retrieval=False)，和推論時的 prompt 逐字相同；
圖是 data/lora/images/<id>.jpg，和後端送給生成端的圖同一份位元組（長邊 1024 px 的 JPEG）。
外洩檢查：和 eval/qa*.jsonl 全部題目比字元 3-gram Jaccard，超過 0.5 的刪掉。不用 eval/photos/。

輸出到 data/lora/（不進 Git）：train.jsonl、valid.jsonl、review.jsonl（給組員 D 抽查兩成）、
select.jsonl（eval/qa.jsonl 的 dev 題，挑 epoch 用）、report.json（各類數量、外洩檢查結果）、
images/。

用法：
    python pipelines/make_lora_data.py
        [--gen-url http://localhost:11434/v1 --gen-model qwen3-vl:4b-instruct]
        [--base-url ... --base-model ...]   # retain 題的答案；預設同 --gen-*
        [--limit 10]                        # 只產生 N 筆 fact（跑通整條流程用）
"""

import argparse
import base64
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import REPO_ROOT  # noqa: E402
from app.rag.preprocess import load_image, to_jpeg_bytes  # noqa: E402
from app.rag.prompt import build_messages  # noqa: E402
from app.rag.providers import is_local_url  # noqa: E402

KB = REPO_ROOT / "kb" / "artworks"
EVAL = REPO_ROOT / "eval"
OUT = REPO_ROOT / "data" / "lora"
REFUSAL_ANSWER = "知識庫中沒有這方面的資料。"
MIX = {"fact": 0.75, "refusal": 0.15, "retain": 0.10}
LEAK_THRESHOLD = 0.5
# 答案的字元 3-gram 至少這麼多比例出現在段落裡，才算有依據（擋掉地端模型自己加料的答案）
SUPPORT_THRESHOLD = 0.35

# 知識庫沒有記載的面向；KEYWORDS 出現在該畫作 JSON 裡就不對那幅畫出這題
REFUSAL_ASPECTS = [
    ("最近一次清潔保養是在哪一年", ("清潔", "保養")),
    ("展出時展廳的溫度設定是幾度", ("溫度",)),
    ("畫家畫這幅畫時一共用了幾支畫筆", ("畫筆",)),
    ("畫布背面貼了哪些標籤", ("背面", "標籤")),
    ("第一次被拍照記錄是哪一年", ("拍照", "攝影")),
    ("畫家完成這幅畫那天的天氣如何", ("天氣",)),
    ("現在每天大約有多少人來看", ("參觀人數", "人次")),
    ("館方用什麼燈光照明", ("燈光", "照明")),
    ("數位掃描的解析度是多少", ("掃描", "解析度")),
    ("負責修復的修復師叫什麼名字", ("修復",)),
    ("畫家用的顏料是在哪家店買的", ("店",)),
    ("重量大約是幾公斤", ("公斤", "重量")),
]
REFUSAL_SUBJECTS = ("這幅畫", "〈{title}〉", "這件作品")


@dataclass(frozen=True)
class Passage:
    artwork_id: str
    kind: str  # card／description／region
    topic: str
    text: str


# ---------------------------------------------------------------- 知識庫 → 段落


def load_artworks() -> list[dict]:
    return [json.loads(p.read_text("utf-8")) for p in sorted(KB.glob("*.json"))]


def card_text(a: dict) -> str:
    # 英文名可省略（臺灣民間藝術沒有英文名），寫法同 app/rag/chunking.py
    title_en = f"（{a['title']['en']}）" if a["title"].get("en") else ""
    artist_en = f"（{a['artist']['en']}）" if a["artist"].get("en") else ""
    return (
        f"作品資料：標題〈{a['title']['zh']}〉{title_en}；"
        f"作者{a['artist']['zh']}{artist_en}；年代 {a['date_text']}；"
        f"媒材 {a.get('medium', '')}；尺寸 {a.get('dimensions', '')}；典藏 {a['collection']}；"
        f"風格標籤 {'、'.join(a.get('style_tags', []))}。"
    )


def region_position(points: list[list[float]]) -> str:
    x = sum(p[0] for p in points) / len(points)
    y = sum(p[1] for p in points) / len(points)
    row = "上" if y < 1 / 3 else "下" if y > 2 / 3 else "中"
    col = "左" if x < 1 / 3 else "右" if x > 2 / 3 else "中"
    if row == "中":
        return "畫面正中央" if col == "中" else f"畫面{col}側"
    return f"畫面{'' if col == '中' else col}{row}方"


def passages(a: dict) -> list[Passage]:
    """作品卡、已審核的 descriptions、regions（區域位置＋綁在該區域的段落）。"""
    out = [Passage(a["id"], "card", "作品卡", card_text(a))]
    reviewed = [d for d in a["descriptions"] if "未審核" not in d["topic"]]
    out += [Passage(a["id"], "description", d["topic"], d["text"]) for d in reviewed]
    for r in (a.get("regions") or {}).get("items", []):
        bound = " ".join(d["text"] for d in reviewed if d.get("region") == r["id"])
        text = f"畫面上標記的區域「{r['label']}」位在{region_position(r['points'])}。{bound}"
        out.append(Passage(a["id"], "region", f"區域：{r['label']}", text.strip()))
    return out


# ---------------------------------------------------------------- 外洩檢查


def char_ngrams(text: str, n: int = 3) -> set[str]:
    s = re.sub(r"[\W_]+", "", text.lower())
    return {s[i : i + n] for i in range(len(s) - n + 1)} if len(s) >= n else {s} if s else set()


def jaccard(a: str, b: str) -> float:
    x, y = char_ngrams(a), char_ngrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def eval_questions(eval_dir: Path = EVAL) -> list[str]:
    return [
        json.loads(line)["question"]
        for path in sorted(eval_dir.glob("qa*.jsonl"))
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


class LeakCheck:
    """和任何評估題目的字元 3-gram Jaccard > threshold 就不收；累計整份資料的外洩報告。
    fact 題在產生時就過這關，每段題數才是外洩過濾後的數量。"""

    def __init__(self, questions: list[str], threshold: float = LEAK_THRESHOLD):
        self.questions, self.threshold = questions, threshold
        self.candidates, self.removed, self.best_all, self.best_kept = 0, [], 0.0, 0.0

    def admit(self, question: str) -> bool:
        score, closest = max(((jaccard(question, q), q) for q in self.questions), default=(0, ""))
        self.candidates += 1
        self.best_all = max(self.best_all, score)
        if score > self.threshold:
            self.removed.append({"question": question, "eval_question": closest, "jaccard": score})
            return False
        self.best_kept = max(self.best_kept, score)
        return True

    def report(self) -> dict:
        return {
            "threshold": self.threshold,
            "eval_questions": len(self.questions),
            "candidates": self.candidates,
            "removed": len(self.removed),
            "removed_items": sorted(self.removed, key=lambda r: -r["jaccard"]),
            "max_jaccard": round(self.best_all, 4),
            "max_jaccard_kept": round(self.best_kept, 4),
        }


# ---------------------------------------------------------------- 樣本


def training_image(a: dict, out: Path) -> Path:
    """和後端送給生成端的圖同一份位元組（chat_service：to_jpeg_bytes(load_image(...))）。"""
    path = out / "images" / f"{a['id']}.jpg"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(to_jpeg_bytes(load_image(REPO_ROOT / a["image"]["path"])))
    return path


def make_sample(
    kind: str,
    question: str,
    answer: str,
    artwork: dict | None,
    image: Path | None,
    out: Path,
    passage: Passage | None = None,
) -> dict:
    """推論時的 messages（build_messages、關檢索）＋答案。

    圖片不存 base64：user content 的 image_url 換成 {"type": "image"}，
    路徑放 images（mlx-vlm 的資料格式）。"""
    jpeg = image.read_bytes() if image else None
    messages = build_messages(question, artwork, [], jpeg, use_retrieval=False)
    messages[1]["content"] = [
        {"type": "image"} if part["type"] == "image_url" else part
        for part in messages[1]["content"]
    ]
    return {
        "kind": kind,
        "artwork_id": artwork["id"] if artwork else None,
        "passage": {"kind": passage.kind, "topic": passage.topic} if passage else None,
        "question": question,
        "answer": answer,
        "images": [image.relative_to(out).as_posix()] if image else [],
        "messages": [*messages, {"role": "assistant", "content": answer}],
    }


def as_inference_messages(sample: dict, out: Path) -> list[dict]:
    """把樣本換回推論時送出的 messages（{"type": "image"} → base64 image_url），測試用。"""
    messages = [dict(m) for m in sample["messages"][:2]]
    images = iter(sample["images"])
    messages[1]["content"] = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,"
                + base64.b64encode((out / next(images)).read_bytes()).decode()
            },
        }
        if part["type"] == "image"
        else part
        for part in messages[1]["content"]
    ]
    return messages


def support(answer: str, text: str) -> float:
    a = char_ngrams(re.sub(r"\[\d+\]", "", answer))
    return len(a & char_ngrams(text)) / len(a) if a else 0.0


def with_no_source_mark(answer: str) -> str:
    """關檢索的 prompt 要求句尾標 [0]（NO_CONTEXT）：拿掉其他編號，結尾補一個 [0]。"""
    return re.sub(r"\s*\[\d+\]", "", answer).strip() + " [0]"


def mix_counts(n_fact: int) -> tuple[int, int]:
    total = n_fact / MIX["fact"]
    return round(total * MIX["refusal"]), round(total * MIX["retain"])


def refusal_questions(a: dict) -> list[str]:
    text = json.dumps(a, ensure_ascii=False)
    return [
        f"{subject.format(title=a['title']['zh'])}{aspect}？"
        for aspect, keywords in REFUSAL_ASPECTS
        if not any(k in text for k in keywords)
        for subject in REFUSAL_SUBJECTS
    ]


def forget_terms(eval_dir: Path = EVAL) -> set[str]:
    """遺忘評估（qa_general）的作品名與答案：retain 題碰到就不用，免得把考題教進權重。"""
    terms = set()
    for line in (eval_dir / "qa_general.jsonl").read_text("utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            terms |= set(re.findall(r"〈(.+?)〉", q["question"]))
            terms |= {k for group in q["keywords"] for k in group if len(k) >= 2}
    return terms


def selection_samples(artworks: list[dict], out: Path, eval_dir: Path = EVAL) -> list[dict]:
    """eval/qa.jsonl 的 dev 題（知識庫內的畫作）：挑 epoch 用，格式和訓練樣本相同、沒有答案。

    dev 只用來挑 epoch，test 只在 make eval-lora 跑一次。"""
    by_id = {a["id"]: a for a in artworks}
    rows = []
    for line in (eval_dir / "qa.jsonl").read_text("utf-8").splitlines():
        q = json.loads(line) if line.strip() else None
        if not q or q["split"] != "dev" or q.get("artwork_id") not in by_id:
            continue
        a = by_id[q["artwork_id"]]
        s = make_sample("select", q["question"], "", a, training_image(a, out), out)
        rows.append({**s, "id": q["id"], "messages": s["messages"][:2], "keywords": q["keywords"]})
    return rows


# ---------------------------------------------------------------- 地端模型


class LocalLLM:
    """OpenAI 相容介面；只准連本機或內網（不得用雲端 API 產生訓練資料）。"""

    def __init__(self, base_url: str, model: str):
        if not is_local_url(base_url):
            raise SystemExit(f"{base_url} 不是本機或內網位址：訓練資料只能用地端模型產生")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.client = httpx.Client(timeout=600)

    def chat(self, messages: list[dict], temperature: float, max_tokens: int = 2000) -> str:
        r = self.client.post(
            self.url,
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            headers={"Authorization": "Bearer local"},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def parse_items(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, flags=re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("question") and i.get("answer")]


def fact_qa(
    llm: LocalLLM,
    p: Passage,
    a: dict,
    target: int,
    rng: random.Random,
    admit=lambda q: True,
    max_rounds: int = 40,
) -> list[tuple]:
    """依段落產生最多 target 組不重複的問答；答案要有段落依據，問題要通過外洩檢查（admit）。
    短段落（例如作品卡）一輪常只收到幾題，所以最多問 max_rounds 輪。"""
    found: dict[str, str] = {}
    for _ in range(max_rounds):
        if len(found) >= target:
            break
        ask = min(10, target - len(found) + 2)
        prompt = (
            f"畫作：〈{a['title']['zh']}〉，{a['artist']['zh']}。\n"
            f"段落主題：{p.topic}\n段落：{p.text}\n\n"
            f"請只根據這段文字，寫出 {ask} 組觀眾可能會問的問題與回答。\n"
            "- 問法要彼此不同：直接問、口語、換句話說、只問其中一個細節都可以；"
            "可以用「這幅畫」稱呼，也可以寫出畫名\n"
            "- 回答用繁體中文（台灣用語）2–4 句，內容必須能在段落裡找到，不可以加段落沒有的事\n"
            '- 只輸出 JSON 陣列：[{"question": "…", "answer": "…"}]'
        )
        temperature = rng.choice([0.7, 0.8, 0.9])
        for item in parse_items(llm.chat([{"role": "user", "content": prompt}], temperature)):
            q, ans = item["question"].strip(), item["answer"].strip()
            key = re.sub(r"[\W_]+", "", q)
            if (
                key
                and key not in found
                and support(ans, p.text + card_text(a)) >= SUPPORT_THRESHOLD
                and admit(q)
            ):
                found[key] = (q, with_no_source_mark(ans))
    return list(found.values())[:target]


def retain_questions(llm: LocalLLM, n: int, avoid: set[str], rng: random.Random) -> list[str]:
    blocked = avoid | {"秀拉", "梵谷", "范寬", "大碗島", "絲柏", "谿山", "麥田"}
    found: dict[str, str] = {}
    for _ in range(n // 10 + 8):
        if len(found) >= n:
            break
        prompt = (
            "請列出 15 個一般大眾會問的藝術常識問題（中外畫家、名作、畫派、技法、媒材都可以），"
            "用繁體中文（台灣用語），每題一句。只輸出 JSON 陣列："
            '[{"question": "…", "answer": "-"}]'
        )
        for item in parse_items(llm.chat([{"role": "user", "content": prompt}], 0.9)):
            q = item["question"].strip()
            key = re.sub(r"[\W_]+", "", q)
            if key and key not in found and not any(t in q for t in blocked):
                found[key] = q
    questions = list(found.values())
    rng.shuffle(questions)
    return questions[:n]


def check_fact_counts(per_passage: dict[str, int], minimum: int) -> None:
    """每段至少 minimum 種問法（外洩過濾後）；不夠就失敗，不輸出不合規格的資料。"""
    short = {k: n for k, n in per_passage.items() if n < minimum}
    if short:
        detail = "、".join(f"{k} {n} 題" for k, n in short.items())
        raise SystemExit(f"以下段落不到 {minimum} 種問法：{detail}；重跑或換產生模型")


# ---------------------------------------------------------------- 主程式


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen-url", default="http://localhost:11434/v1")
    ap.add_argument("--gen-model", default="qwen3-vl:4b-instruct")
    ap.add_argument("--base-url", default="", help="retain 題的答案（底模）；預設同 --gen-url")
    ap.add_argument("--base-model", default="", help="預設同 --gen-model")
    ap.add_argument("--min-variants", type=int, default=20)
    ap.add_argument("--max-variants", type=int, default=30)
    ap.add_argument("--valid-frac", type=float, default=0.1)
    ap.add_argument("--review-frac", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=0, help="只產生 N 筆 fact（跑通流程用）")
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    gen = LocalLLM(args.gen_url, args.gen_model)
    base = LocalLLM(args.base_url or args.gen_url, args.base_model or args.gen_model)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    artworks = load_artworks()

    leak = LeakCheck(eval_questions())
    facts: list[dict] = []
    per_passage: dict[str, int] = {}
    for a in artworks:
        image = training_image(a, out)
        for p in passages(a):
            target = rng.randint(args.min_variants, args.max_variants)
            if args.limit:
                target = min(target, max(0, args.limit - len(facts)))
            if target <= 0:
                continue
            pairs = fact_qa(gen, p, a, target, rng, admit=leak.admit)
            print(f"[fact] {a['id']} {p.topic}：{len(pairs)}／{target}")
            per_passage[f"{a['id']} {p.topic}"] = len(pairs)
            facts += [make_sample("fact", q, ans, a, image, out, p) for q, ans in pairs]
    if not args.limit:
        check_fact_counts(per_passage, args.min_variants)

    n_refusal, n_retain = mix_counts(len(facts))
    pool = [(a, q) for a in artworks for q in refusal_questions(a)]
    rng.shuffle(pool)
    refusals = [
        make_sample("refusal", q, REFUSAL_ANSWER, a, training_image(a, out), out)
        for a, q in pool[:n_refusal]
    ]

    avoid = forget_terms()
    retains = []
    for q in retain_questions(gen, n_retain * 2, avoid, rng):
        if len(retains) >= n_retain:
            break
        messages = build_messages(q, None, [], None, use_retrieval=False)
        ans = base.chat(messages, temperature=0, max_tokens=800).strip()
        if ans and not any(t in ans for t in avoid):
            retains.append(make_sample("retain", q, ans, None, None, out))
    print(f"[mix] fact {len(facts)}、refusal {len(refusals)}、retain {len(retains)}")

    kept = facts + [s for s in refusals + retains if leak.admit(s["question"])]
    assert all("eval/photos" not in img for s in kept for img in s["images"])
    leak_report = leak.report()
    print(
        f"[leak] 候選 {leak_report['candidates']}、刪除 {leak_report['removed']}、"
        f"最高 Jaccard {leak_report['max_jaccard']}"
    )

    rng.shuffle(kept)
    for i, s in enumerate(kept):
        s["id"] = f"lora-{i:04d}"
    n_valid = max(1, round(len(kept) * args.valid_frac)) if len(kept) > 1 else 0
    valid, train = kept[:n_valid], kept[n_valid:]
    review = rng.sample(kept, max(1, round(len(kept) * args.review_frac))) if kept else []
    select = selection_samples(artworks, out)
    for name, rows in (("train", train), ("valid", valid), ("review", review), ("select", select)):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for s in rows:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    counts = {k: sum(s["kind"] == k for s in kept) for k in MIX}
    report = {
        "seed": args.seed,
        "gen_model": args.gen_model,
        "base_model": args.base_model or args.gen_model,
        "artworks": [a["id"] for a in artworks],
        "passages": {a["id"]: [p.topic for p in passages(a)] for a in artworks},
        "counts": counts,
        "fact_per_passage": per_passage,
        "min_variants": args.min_variants,
        "mix": {k: round(v / len(kept), 3) for k, v in counts.items()} if kept else {},
        "train": len(train),
        "valid": len(valid),
        "review": len(review),
        "select": len(select),
        "leak": leak_report,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(f"寫入 {out}：train {len(train)}、valid {len(valid)}、抽查 {len(review)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
