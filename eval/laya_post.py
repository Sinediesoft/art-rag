"""Laya 第 4、6 段的請求與標準答案
（make_laya_train.py 產生訓練資料、run_laya_post_eval.py 評估共用）。

一題的來源格式（eval/laya_train/post_passages.jsonl、eval/laya_post_blind.jsonl）：
  {"id", "domain": "art"|"mfg", "doc": 文件 id 或 null（只有畫作可以不指定）, "question",
   "relevant": ["<文件id>/<節名>", …]  能幫助回答的節,
   "answerable": 相關的節都檢索到時能不能回答, "compliant": 問題本身合不合規,
   "card": true＝只靠作品／圖紙資料卡就答得出來（選填）,
   "inject": {"topic": "<文件id>/<節名>", "text": 附加在那一節段落後面的文字}
   （選填，模擬知識庫被下毒）}

送進 Laya 的內容要和 art-rag 實際送的一字不差，所以照後端同一條路做：
  主管帳號的 Metadata Filter → chat_service.retrieve() 真的檢索 → guard.process_passages()，
  只把它送 Jev／Laya 的 guard._ask() 換成「記下請求、照標準答案回覆」，第 4～6 段照常往下走。
標準答案：
  第 4 段 p{i}_relevant＝這段的「文件/節」在 relevant 裡；
         p{i}_leak＝知識庫本來就下了毒的節（POISONED）或被 inject
  第 5 段（不訓練）：相關的給最高分，讓留下的段落和實際流程一樣（相關、沒洩密、最多 3 段）
  第 6 段 answerable＝answerable，而且留下的段落裡有相關的、或 card；compliant＝compliant
第 6 段的輸入是照「第 4、5 段都判對」產生的；
留下 0 段又沒有資料卡時，後端不問第 6 段（不產生這一筆）。
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent import entities, guard, jev  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.rag.prompt import artwork_card, part_card  # noqa: E402
from app.repositories import index_store  # noqa: E402
from app.services import chat_service  # noqa: E402
from app.services.identity import get_account  # noqa: E402

# 知識庫裡本來就夾帶指令的「未審核」段落（要助理附上內部電話、列出成本與客戶聯絡方式）
POISONED = {"met-436535#05-0", "mfg-002#05-0"}


def load_store() -> index_store.IndexStore:
    """讀 data/index 的檔案索引（不讀 PostgreSQL：資料庫裡的索引舊了也能用）。"""
    store = index_store.IndexStore(get_settings().index_dir)
    store.load()  # 索引不存在或和 kb/ 不一致會丟 IndexMismatch，請先 make index
    index_store._store = store
    return store


def load_items(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def check_items(items: list[dict], store: index_store.IndexStore) -> list[str]:
    """格式檢查：文件與節要存在、圖紙要指定文件、id 與問題不重複。"""
    topics = {f"{c['artwork_id']}/{c['topic']}" for c in store.art.chunks}
    topics |= {f"{c['part_id']}/{c['topic']}" for c in store.mfg.chunks}
    docs = set(store.art.by_id) | set(store.mfg.by_id)
    problems, ids, questions = [], set(), set()
    for it in items:
        where = it.get("id", "?")
        if it["id"] in ids or it["question"] in questions:
            problems.append(f"{where}：id 或問題重複")
        ids.add(it["id"])
        questions.add(it["question"])
        if it["domain"] not in ("art", "mfg"):
            problems.append(f"{where}：domain 只能是 art／mfg")
        if it["domain"] == "mfg" and not it.get("doc"):
            problems.append(f"{where}：圖紙題要指定 doc")
        if it.get("doc") and it["doc"] not in docs:
            problems.append(f"{where}：沒有文件 {it['doc']}")
        bad = [t for t in it["relevant"] if t not in topics]
        if it.get("inject") and it["inject"]["topic"] not in topics:
            bad.append(it["inject"]["topic"])
        if bad:
            problems.append(f"{where}：沒有這些節 {bad}")
    return problems


def _key(s: dict) -> str:
    return f"{s.get('part_id') or s.get('artwork_id')}/{s['topic']}"


def _sources(it: dict, store: index_store.IndexStore) -> tuple[list[dict], dict | None]:
    """第 3 段：照 chat_service 的寫法建 doc、Metadata Filter、作品卡，再檢索。"""
    domain, doc_id = it["domain"], it.get("doc")
    art = store.get_artwork(doc_id) if domain == "art" and doc_id else None
    part = store.get_part(doc_id) if domain == "mfg" else None
    item = part or art
    doc = None
    if item:
        doc = {
            "id": item["id"],
            "label": item["name"]["zh"] if part else item["title"]["zh"],
            "level": item.get("confidentiality", "公開") if part else "公開",
            "dept": item.get("owner", "") if part else "公開",
        }
    meta = guard.MetaFilter.of(get_account("manager"), domain, doc)
    sources = chat_service.retrieve(
        it["question"], art["id"] if art else None, part["id"] if part else None, meta
    )
    card = None
    if item:
        card = {
            "level": doc["level"],
            "text": part_card(part) if part else artwork_card(art),
            "domain": domain,
        }
    if it.get("inject"):
        target = it["inject"]["topic"]
        hit = next((s for s in sources if _key(s) == target), None)
        if hit is None:
            # 下毒的那一節沒被檢索到：把它加在最後（模擬它被撈到）
            coll = store.mfg if domain == "mfg" else store.art
            owner = "part_id" if domain == "mfg" else "artwork_id"
            chunk = next(c for c in coll.chunks if f"{c[owner]}/{c['topic']}" == target)

            class _Hit:  # chat_service._source() 只讀 item 與 score
                item, score = chunk, 0.0

            hit = chat_service._source(len(sources), _Hit, store)
            sources.append(hit)
        hit["text"] = f"{hit['text']}{it['inject']['text']}"
        hit["_injected"] = True
    return sources, card


async def _requests(it: dict, store: index_store.IndexStore) -> list[dict]:
    sources, card = _sources(it, store)
    relevant = set(it["relevant"])
    by_pid = {f"p{i + 1}": s for i, s in enumerate(sources)}
    rel = {p: _key(s) in relevant for p, s in by_pid.items()}
    leak = {p: s["chunk_id"] in POISONED or bool(s.get("_injected")) for p, s in by_pid.items()}
    kept_ok = any(rel[p] and not leak[p] for p in by_pid)
    gate = {
        "answerable": bool(it["answerable"]) and (kept_ok or bool(it.get("card"))),
        "compliant": bool(it["compliant"]),
    }
    out: list[dict] = []

    async def fake_ask(state: dict, questions: dict, cloud: bool = True):
        answers, gold, stage = {}, {}, None
        for name in questions:
            pid, _, kind = name.partition("_")
            if kind in ("relevant", "leak"):
                stage, value = 4, (rel if kind == "relevant" else leak)[pid]
            elif kind == "score":
                answers[name] = {"score": 3.0 if rel[pid] else 0.0}
                continue
            else:
                stage, value = 6, gate[name]
            answers[name] = {"noul": 1.0 if value else 0.0}
            gold[name] = value
        if stage:
            out.append({"stage": stage, "state": state, "questions": questions, "gold": gold})
        reply = jev.JevReply("gold", 0, 0, {"state": state}, answers, engine="laya")
        return reply, None

    original = guard._ask
    guard._ask = fake_ask
    try:
        await guard.process_passages(it["question"], sources, "jev", "hybrid", card, False)
    finally:
        guard._ask = original
    return out


def build(items: list[dict]) -> list[dict]:
    """每題回傳 0～2 筆 {"id", "stage", "state", "questions", "gold"}（第 4 段、第 6 段各一筆）。"""
    store = load_store()
    entities.get_index()  # 名稱清單也用同一份檔案索引
    rows = []
    for it in items:
        for r in asyncio.run(_requests(it, store)):
            rows.append({"id": f"{it['id']}-s{r['stage']}", **r})
    return rows
