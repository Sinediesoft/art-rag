"""自動出「要兩段以上才答得完整」的問答評估題（Q-2026-10-05-05 第 1 項，docs/adr/008）。

借 Kaggle LLM Science Exam 的出題方式：比賽的測試題是 GPT-3.5 依 Wikipedia 段落出的，
前幾名也自己用 LLM 出了幾萬題當驗證資料。這裡用本地生成端（hybrid，Qwen3-VL）從知識庫段落出題，
每題出題時就知道要用到哪幾段（gold_chunks），檢索、Rearrange 有沒有留下它們可以直接量。

兩種題（type）：
- multi：同一幅畫的兩段（跨段落題），例如「在哪裡畫的？後來又畫了哪些版本？」
- compare：兩幅畫同一類的段落（比較題），站在第一幅前面問，題目寫出第二幅的標題；
  要含「比較／不同」這類字眼，檢索才會從全庫補段落（models.yaml 的 global_fill_keywords）

不出「基本資料」段（#meta）：回答的 prompt 一定會帶畫作卡（標題、畫家、年代、材質、收藏），
就算檢索沒留下這段也答得出來，量不到篩選有沒有誤刪。也不出沒有網址出處的投稿段落（沒審核過）。

模型出的題先過程式檢查，不合格的關鍵字丟掉，丟完某一段沒有關鍵字的整題不合格：
1. 關鍵字要逐字出現在它那一段（OpenCC 轉成台灣繁體之後比對）；
2. 不能也出現在另一段：否則只看一段就答得出來；
3. 不能出現在題目裡（洩漏答案），也不能出現在畫作卡裡（不用檢索就寫得出來）。
合格的題仍要人工看過（題意清不清楚、是不是真的要兩段），再把那一行複製進 eval/qa.jsonl
（去掉 _ 開頭的欄位）。關鍵字每段一組、組內任一個出現就算：答案要兩段的資訊都有才算對。

段落讀 data/index/chunks.json（make index 產生，後端檢索的就是這一份）。
候選題寫到 data/qa_gen/<run_id>-multi.jsonl（不 commit）。
用法：python eval/make_qa_multi.py [--artworks met-436535,...] [--types multi,compare] [--limit N]
"""

import argparse
import asyncio
import functools
import itertools
import json
import sys
import time
import uuid
import zlib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_models_config  # noqa: E402
from app.rag.prompt import artwork_card  # noqa: E402
from app.rag.providers import ProviderUnavailable, get_provider  # noqa: E402
from app.rag.textproc import to_taiwan  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
INDEX = ROOT / "data" / "index"
OUT = ROOT / "data" / "qa_gen"
GEN = "make_qa_multi v1"

# 比較題只配同一類的段落（topic 名稱各畫不同，用關鍵字歸類）；沒有歸到類的段落不出比較題
COMPARE_GROUPS = {
    "畫面": ("畫面", "構圖"),
    "技法": ("技法",),
    "收藏": ("收藏",),
    "色彩": ("色彩分析",),
}

SYSTEM = (
    "你是美術館導覽系統的評估出題者。你會拿到兩段資料 A、B，"
    "要出一題「一定要同時用到 A 和 B 才答得完整」的問題。只輸出 JSON。"
)
# 依序產生：先挑答案詞、再寫題目。4B 模型先寫題目時常把答案寫進題目裡
# （2026-10-05 試跑：「這幅畫是梵谷在聖雷米療養院外寫生…有何意義？」），先挑詞才知道哪些字不能寫
RULES = (
    "步驟：\n"
    "1. keywords_a：從 A 原文逐字複製 1–3 個詞，是這題正確答案一定要提到的"
    "（專有名詞、人名、地名、年份、數字、術語，2–8 個字），這些詞不能出現在 B 裡。\n"
    "2. keywords_b：同樣從 B 原文逐字複製 1–3 個詞，不能出現在 A 裡。\n"
    "3. question：{who}題目要問到 keywords_a 和 keywords_b，但題目裡絕對不能出現這些詞，"
    "也不能出現 A、B 裡的其他答案。只看 A 或只看 B 都不能答完整。\n"
    "4. answer：只根據 A、B，寫 1–3 句參考答案，要包含上面的關鍵字。\n"
    "一律使用繁體中文。\n"
    "\n"
    "示範（另一幅畫，格式參考用）：\n"
    "A：這幅畫是畫家 1890 年在巴黎北邊的奧維小鎮畫的，那時他住在嘉舍醫師家附近。\n"
    "B：這幅畫 1952 年由收藏家捐贈，現藏巴黎奧塞美術館。\n"
    '輸出：{{"keywords_a": ["奧維", "嘉舍醫師"], "keywords_b": ["奧塞美術館", "1952"], '
    '"question": "這幅畫是在哪裡畫的？後來又是怎麼進到現在收藏它的美術館？", '
    '"answer": "畫家在巴黎北邊的奧維小鎮畫了這幅畫，當時住在嘉舍醫師家附近；'
    '1952 年由收藏家捐贈給巴黎奧塞美術館。"}}'
)
WHO_MULTI = (
    "要像觀眾站在畫前會問的話，用「這幅畫」稱呼它，15–45 字；"
    "可以一句問兩件事，也可以是要把兩段串起來才答得出來的問題。"
)
WHO_COMPARE = (
    "觀眾站在畫作一前面問：用「這幅畫」稱呼畫作一，畫作二寫出完整標題〈{title_b}〉，"
    "並用「比較」「不同」或「差異」這類字眼，20–50 字。"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "keywords_a": {"type": "array", "items": {"type": "string"}},
        "keywords_b": {"type": "array", "items": {"type": "string"}},
        "question": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["keywords_a", "keywords_b", "question", "answer"],
    "additionalProperties": False,
}


def load_index() -> tuple[dict[str, dict], dict[str, list[dict]]]:
    if not (INDEX / "chunks.json").exists():
        raise SystemExit("找不到 data/index/chunks.json，請先執行 make index")
    arts = {a["id"]: a for a in json.loads((INDEX / "artworks.json").read_text("utf-8"))}
    chunks: dict[str, list[dict]] = {}
    for c in json.loads((INDEX / "chunks.json").read_text("utf-8")):
        if c.get("artwork_id") not in arts or c["chunk_id"].endswith("#meta"):
            continue
        # 沒有網址的出處＝使用者投稿、外部上傳的文件（例如 kb 2026.10.1 示範用的被汙染觀眾留言）：
        # 沒經過審核，不拿來出題；系統算的色彩分析也沒有網址，照出
        if c.get("source") and not c["chunk_id"].endswith("#color"):
            continue
        chunks.setdefault(c["artwork_id"], []).append(c)
    return arts, chunks


def suffix(chunk_id: str) -> str:
    """met-436535#01-0 → 01；met-436535#color → color（題目編號用）。"""
    s = chunk_id.split("#", 1)[1]
    return s.split("-")[0] if s[0].isdigit() else s


def compare_group(topic: str) -> str | None:
    return next((g for g, words in COMPARE_GROUPS.items() if any(w in topic for w in words)), None)


def pairs(arts: dict, chunks: dict, types: set[str], only: set[str] | None) -> list[dict]:
    jobs = []
    ids = [a for a in arts if a in chunks and (not only or a in only)]
    if "multi" in types:
        for aid in ids:
            for a, b in itertools.combinations(chunks[aid], 2):
                qid = f"qm-{aid}-{suffix(a['chunk_id'])}-{suffix(b['chunk_id'])}"
                jobs.append({"id": qid, "type": "multi", "artwork_id": aid, "a": a, "b": b})
    if "compare" in types:
        for x, y in itertools.combinations(ids, 2):
            for a in chunks[x]:
                for b in chunks[y]:
                    g = compare_group(a["topic"])
                    if g and g == compare_group(b["topic"]):
                        sa, sb = suffix(a["chunk_id"]), suffix(b["chunk_id"])
                        qid = f"qc-{x}-{sa}-{y}-{sb}"
                        jobs.append({"id": qid, "type": "compare", "artwork_id": x, "a": a, "b": b})
    return jobs


def messages(job: dict, arts: dict) -> list[dict]:
    a, b = job["a"], job["b"]
    ta = arts[a["artwork_id"]]["title"]["zh"]
    tb = arts[b["artwork_id"]]["title"]["zh"]
    if job["type"] == "multi":
        head = (
            f"畫作：〈{ta}〉\n\n"
            f"【A】（{a['topic']}）{a['text']}\n\n【B】（{b['topic']}）{b['text']}"
        )
        who = WHO_MULTI
    else:
        head = (
            f"畫作一：〈{ta}〉\n畫作二：〈{tb}〉\n\n"
            f"【A】（〈{ta}〉・{a['topic']}）{a['text']}\n\n【B】（〈{tb}〉・{b['topic']}）{b['text']}"
        )
        who = WHO_COMPARE.format(title_b=tb)
    user = head + "\n\n" + RULES.format(who=who)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [{"type": "text", "text": user}]},
    ]


def drop_reason(k: str, own: str, other: str, question: str, cards: str) -> str | None:
    """關鍵字不能用的原因；能用回 None。"""
    if len(k) < 2:
        return "太短"
    if k not in own:
        return "不在原段"
    if k in other:
        return "另一段也有"
    if k in question:
        return "題目洩漏"
    if k in cards:
        return "畫作卡就有"
    return None


def check(job: dict, raw: dict, arts: dict) -> dict:
    """程式檢查：回傳 {question, keywords, problems, dropped}；problems 非空＝不合格。"""
    a, b = job["a"], job["b"]
    question = to_taiwan(raw.get("question", "")).strip()
    cards = artwork_card(arts[a["artwork_id"]]) + artwork_card(arts[b["artwork_id"]])
    groups, dropped, problems = [], [], []
    for side, own, other in (("a", a, b), ("b", b, a)):
        kept = []
        for k in dict.fromkeys(to_taiwan(k).strip() for k in raw.get(f"keywords_{side}", [])):
            why = drop_reason(k, own["text"], other["text"], question, cards)
            if why:
                dropped.append(f"{side}:{k}（{why}）")
            else:
                kept.append(k)
        if not kept:
            problems.append(f"{side} 段沒有可用的關鍵字")
        groups.append(kept)
    if not question:
        problems.append("沒有題目")
    if job["type"] == "compare":
        fill = get_models_config().global_fill_keywords
        if not any(w in question for w in fill):
            problems.append("沒有比較字眼（不會從全庫補段落）")
        title_b = arts[b["artwork_id"]]["title"]["zh"]
        if title_b not in question:
            problems.append(f"沒寫出〈{title_b}〉")
    return {"question": question, "keywords": groups, "problems": problems, "dropped": dropped}


async def generate(job: dict, arts: dict) -> tuple[dict, dict]:
    provider = get_provider("hybrid")
    provider.max_tokens, provider.temperature = 400, 0.0
    provider.response_format = {
        "type": "json_schema",
        "json_schema": {"name": "qa_multi", "schema": SCHEMA, "strict": True},
    }
    t0 = time.perf_counter()
    text = "".join([p async for p in provider.stream(messages(job, arts))])
    info = {"model": provider.model, "ms": round((time.perf_counter() - t0) * 1000)}
    try:
        return json.loads(text), info
    except json.JSONDecodeError:
        return {}, {**info, "unparsed": text[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artworks", default="", help="只出這幾幅畫的題（逗號分隔），預設全部")
    ap.add_argument("--types", default="multi,compare")
    ap.add_argument("--limit", type=int, default=0, help="只出前 N 組（試跑用）")
    args = ap.parse_args()
    arts, chunks = load_index()
    only = {s for s in args.artworks.split(",") if s} or None
    jobs = pairs(arts, chunks, set(args.types.split(",")), only)
    existing = {
        json.loads(line)["id"]
        for line in (ROOT / "eval" / "qa.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    }
    jobs = [j for j in jobs if j["id"] not in existing]  # 已收進 qa.jsonl 的不重出
    jobs = jobs[: args.limit] if args.limit else jobs
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{run_id}-multi.jsonl"
    print(f"出題 {run_id}：{len(jobs)} 組段落 → {out.relative_to(ROOT)}")
    n_ok = 0
    with out.open("w", encoding="utf-8") as f:
        for i, job in enumerate(jobs, 1):
            try:
                raw, info = asyncio.run(generate(job, arts))
            except ProviderUnavailable as e:
                raise SystemExit(f"本地生成端無法使用：{e}（請先啟動 Ollama）") from e
            c = check(job, raw, arts)
            a, b = job["a"], job["b"]
            row = {
                "id": job["id"],
                # 和現有題目一樣約 1/3 當 dev；依編號決定，重出同一題分到同一邊
                "split": "dev" if zlib.crc32(job["id"].encode()) % 3 == 0 else "test",
                "artwork_id": job["artwork_id"],
                "question": c["question"],
                "keywords": c["keywords"],
                "topics": list(dict.fromkeys([a["topic"], b["topic"]])),
                "type": job["type"],
                "gold_chunks": [a["chunk_id"], b["chunk_id"]],
                "gen": f"{GEN}・{info['model']}",
                "_ok": not c["problems"],
                "_problems": c["problems"],
                "_dropped": c["dropped"],
                "_answer": to_taiwan(raw.get("answer", "")),
                "_ms": info["ms"],
                **({"_unparsed": info["unparsed"]} if "unparsed" in info else {}),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_ok += row["_ok"]
            mark = "✓" if row["_ok"] else "✗"
            problems = c["problems"] or ""
            print(f"  [{i}/{len(jobs)}] {mark} {job['id']} {c['question'][:50]} {problems}")
    print(
        f"程式檢查合格 {n_ok}/{len(jobs)}；"
        "人工看過再把合格的那幾行複製進 eval/qa.jsonl（去掉 _ 開頭的欄位）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
