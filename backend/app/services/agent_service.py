"""智慧助理（統一入口）的路由：System 1 判斷意圖與信心 → 信心閘門 → 告訴前端交給哪個 System 2 模組。

流程（docs/adr/007 主流程圖）：
1. 本機前處理：帶入目前身分；找出零件、倉庫、客戶、畫作、單號並換成代號
2. 有附照片：本機 Chinese-CLIP 辨識是畫作還是圖紙（照片不送 Jev）；只有照片沒打字就不呼叫 Jev
3. System 1：有金鑰就問 Jev（只送代號化文字，記錄外送位元組）；沒金鑰或失敗改走本地路由
4. 信心閘門：唯讀直接執行、耗時先確認、修改進入修改資料流程、不確定就出澄清按鈕
5. 通知記憶體管理預估要用的模型；寫路由紀錄（eval-route 也用同一個端點）

這裡只做判斷與分派；實際執行沿用各模組原本的 API（/chat、/inventory/ask、/changes/preview…），
那些 API 各自做自己的安全檢查，所以前端分派錯了也繞不過權限。
"""

import asyncio
import time

from app.agent import gate, jev, local_router
from app.agent.entities import first, get_index
from app.agent.types import System1Result
from app.core.config import get_agent_config
from app.core.logging import log
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services import memory_guard, search_service
from app.services.identity import Account

PHOTO_INTENTS = {"art": "art_qa", "drawing": "drawing_qa"}


def _identify_photo(image_id: str) -> dict:
    """照片是畫作還是圖紙：兩種辨識都跑，通過驗證的那一個；都沒過就是無法辨識。"""
    art = search_service.identify(image_id)
    if art["matched"]:
        a = get_store().get_artwork(art["best_artwork_id"])
        return {"kind": "art", "id": a["id"], "label": a["title"]["zh"]}
    drawing = search_service.identify_drawing(image_id)
    if drawing["matched"]:
        p = get_store().get_part(drawing["best_part_id"])
        return {"kind": "drawing", "id": p["id"], "label": p["name"]["zh"]}
    return {"kind": "unknown", "id": None, "label": "知識庫中沒有這張照片的畫作或圖紙"}


def _dispatch(intent: str, question: str, entities: list, photo: dict | None) -> dict:
    """交給哪個模組、帶什麼參數（實體都在本機解析，不靠 Jev）。"""
    part = first(entities, "part")
    art = first(entities, "artwork") or first(entities, "artist")
    part_id = part.id if part else (photo["id"] if photo and photo["kind"] == "drawing" else None)
    artwork_id = art.id if art else (photo["id"] if photo and photo["kind"] == "art" else None)
    d: dict = {"module": intent, "part_id": part_id, "artwork_id": artwork_id, "question": question}
    store = get_store()
    if part_id and (p := store.get_part(part_id)):
        d["part_label"] = p["name"]["zh"]
    if artwork_id and (a := store.get_artwork(artwork_id)):
        d["artwork_label"] = a["title"]["zh"]
    if intent == "data_query" and photo and photo["kind"] == "drawing" and not part:
        # 「這張圖還剩幾件」：照片辨識出的圖紙名稱補進 Text-to-SQL 的問題
        d["question"] = f"〈{photo['label']}〉{question}"
    if intent == "reconstruct":
        d["path"] = f"/drawings/{part_id}/reconstruct" if part_id else "/reconstruct"
    elif intent == "schedule":
        d["path"] = "/schedule"
    elif intent == "drawing_search" and part_id:
        d["path"] = f"/drawings/{part_id}"
    elif intent == "system":
        d["path"] = "/admin#memory"
    return d


def _permission(intent: str, account: Account) -> tuple[bool, str]:
    """排程與修改先告訴使用者目前身分能不能做（真正的檢查在各自的 API）。"""
    if intent == "schedule" and not account.can("schedule_run"):
        return False, f"「{account.label}」不能執行排程（只有生管可以），請在頁首切換身分"
    if intent == "modify" and not any(o.startswith(("stock_", "so_", "wo_")) for o in account.ops):
        return False, f"「{account.label}」沒有修改資料的權限，下一步權限判定會拒絕並記錄"
    return True, ""


async def route(
    question: str,
    account: Account,
    request_id: str,
    image_id: str | None = None,
    forced_intent: str | None = None,
    engine: str = "auto",
) -> dict:
    t0 = time.perf_counter()
    cfg = get_agent_config()
    question = question.strip()
    photo = await asyncio.to_thread(_identify_photo, image_id) if image_id else None
    photo_kind = photo["kind"] if photo else None
    index = await asyncio.to_thread(get_index)
    entities = index.find(question)
    masked = index.pseudonymize(question, entities)

    fallback_reason = None
    if forced_intent:
        if forced_intent not in cfg["intents"]:
            forced_intent = "out_of_scope"
        op, op_probs = local_router.classify_op(question)
        result = System1Result(
            engine="user",
            intent_probs={k: float(k == forced_intent) for k in cfg["intents"]},
            op_probs=op_probs if op else {},
            model="使用者點選",
        )
    elif not question and photo:
        # 只有照片：本機辨識，不呼叫 Jev
        intent = PHOTO_INTENTS.get(photo_kind or "", "out_of_scope")
        result = System1Result(
            engine="local",
            intent_probs={k: float(k == intent) for k in cfg["intents"]},
            model="Chinese-CLIP 照片辨識",
        )
        fallback_reason = "只有照片：在本機辨識，不呼叫 Jev"
    else:
        result = None
        if engine in ("auto", "jev"):
            try:
                result = await jev.classify(masked.text, photo_kind)
            except jev.JevUnavailable as e:
                fallback_reason = str(e)
        if result is None:
            async with memory_guard.flow("route", {"bge"}):
                result = await asyncio.to_thread(
                    local_router.classify, question, entities, photo_kind
                )
        # 修改操作：Jev 沒判斷出來（或判斷「不是修改」）時用本地關鍵字補
        if result.intent == "modify" and not result.modify_op:
            op, op_probs = local_router.classify_op(question)
            if op:
                result.op_probs = op_probs

    decision = gate.decide(result)
    intent = result.intent
    permitted, permission_note = _permission(intent, account)
    dispatch = _dispatch(intent, question, entities, photo)
    if intent == "modify":
        dispatch["op"] = result.modify_op
        dispatch["op_label"] = cfg["modify_ops"].get(result.modify_op or "", {}).get("label")

    # 記憶體管理：路由結果出來就知道接下來要哪些模型，超過門檻先釋放其他的
    # bge-m3 一律保留：本地路由下一句還要用（釋放後重新載入要 1～2 秒）
    keep = set(cfg["intent_models"].get(intent, [])) | {"bge"}
    label = cfg["intents"][intent]["label"]
    await asyncio.to_thread(memory_guard.guard.check, f"智慧助理預估（{label}）", keep, False, True)

    labels = {k: v["label"] for k, v in cfg["intents"].items()}
    out = {
        "request_id": request_id,
        "account": account.public(),
        "question": question,
        "masked_text": masked.text,
        "mapping": masked.mapping,
        "entities": [
            {"kind": e.kind, "id": e.id, "label": e.label, "text": e.text} for e in entities
        ],
        "photo": photo,
        "engine": result.engine,
        "engine_label": {
            "jev": "Jev（System 1，雲端）",
            "local": "本地路由",
            "user": "使用者點選",
        }[result.engine],
        "model": result.model,
        "fallback_reason": fallback_reason,
        "intent": intent,
        "intent_label": labels[intent],
        "risk": cfg["intents"][intent]["risk"],
        "confidence": round(result.confidence, 4),
        "margin": round(result.margin, 4),
        "jev_confidence": result.jev_confidence,
        "ranked": [
            {"intent": k, "label": labels[k], "prob": round(p, 4)} for k, p in result.ranked[:5]
        ],
        "modify_op": result.modify_op if intent == "modify" else None,
        "flags": result.flags,
        "gate": decision.gate,
        "threshold": decision.threshold,
        "gate_reason": decision.reason,
        "options": [
            {"intent": k, "label": labels[k], "prob": round(p, 4)} for k, p in decision.options
        ],
        "permitted": permitted,
        "permission_note": permission_note,
        "dispatch": dispatch,
        "egress": {
            "bytes": result.egress_bytes,
            "to": "TypeSafe Jev" if result.engine == "jev" else None,
            "images": 0,
        },
        "latency_ms": {
            "system1": result.latency_ms,
            "total": round((time.perf_counter() - t0) * 1000),
        },
        "detail": {k: v for k, v in result.detail.items() if k != "request"},
        "jev_request": result.detail.get("request"),
    }
    get_logs_repo().add_route_log(
        {
            "request_id": request_id,
            "created_at": get_logs_repo().now(),
            "account_id": account.id,
            "question": question,
            "masked_text": masked.text,
            "has_photo": int(bool(image_id)),
            "engine": result.engine,
            "model": result.model,
            "fallback_reason": fallback_reason,
            "intent": intent,
            "modify_op": out["modify_op"],
            "confidence": out["confidence"],
            "margin": out["margin"],
            "gate": decision.gate,
            "overrides_rules": int(bool(result.flags.get("overrides_rules"))),
            "egress_bytes": result.egress_bytes,
            "system1_ms": result.latency_ms,
            "total_ms": out["latency_ms"]["total"],
        }
    )
    log.info(
        "route",
        extra={
            "fields": {
                "request_id": request_id,
                "engine": result.engine,
                "intent": intent,
                "confidence": out["confidence"],
                "gate": decision.gate,
                "egress": out["egress"],
            }
        },
    )
    return out
