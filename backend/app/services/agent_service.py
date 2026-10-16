"""智慧助理（統一入口）：七段權限控管裡，路由這一步負責第 1、2 段（docs/adr/015）。

0. 個資遮蔽：一收到就把手機、Email、身分證換成代號，原值不保留
   （之後的紀錄與分派都用遮蔽後的文字）
1. 認證與授權：JWT 的簽章與效期在 API 閘道就驗過（沒過是 401，根本到不了這裡）；
   這裡用憑證裡的角色檢查「要做的事」能不能做（功能、動作權限），並產生第 3 段的 Metadata Filter。
   要做什麼由本地分流判斷（照片在本機用 Chinese-CLIP 辨識，不送 Jev；文字用關鍵字＋bge-m3）。
   沒過 → 拒絕並記錄，不送 Jev、不碰資料。看不到的圖紙在實體解析就當成不存在
   （不代號化、不分派、回應裡沒有它的 ID 與名稱，docs/adr/033 F）
2. Jev 意圖路由／防護欄（Jev Choice）：只送代號化文字，判斷正常查詢／Prompt 注入／無關閒聊；
   注入 → 拒絕並記錄；閒聊 → 快速短路回覆（不檢索、不生成）。叫不到 Jev 才改地端規則
3～7 由分派到的模組執行（/chat 做 Metadata Filter 檢索、Jev Noul 雙重驗證、Jev Score 重排、
   生成閘門與地端生成；/inventory/ask 唯讀 SQL…），那些 API 自己也驗 JWT 與資料範圍，
   所以前端分派錯了或直接打 API 也繞不過權限。
   畫作問答、圖紙問答放行時發一張交接票（agent/handoff.py）：/chat 憑票沿用第 2 段的判斷，
   沒票或票不符就自己重跑第 2 段（docs/adr/030）。
最後通知記憶體管理預估要用的模型、寫路由紀錄（eval-route 也用同一個端點）。
"""

import asyncio
import dataclasses
import re
import time

from app.agent import art_resolve, catalog, gate, guard, handoff, local_router
from app.agent.entities import first, get_index
from app.agent.types import System1Result
from app.core.config import get_agent_config
from app.core.logging import log
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services import memory_guard, search_service
from app.services.identity import Account, Auth, level_rank, visible_part_ids

PHOTO_INTENTS = {"art": "art_qa", "drawing": "drawing_qa"}
# 只有照片、沒有文字時，問答模組用的預設問句（交接票綁這句話）
# 沒說畫名時要判斷是哪一幅畫的意圖（docs/adr/038）
ART_RESOLVE_INTENTS = {"art_qa", "art_search"}
DEFAULT_QUESTIONS = {"art_qa": "請介紹這幅畫", "drawing_qa": "這張圖紙的重點是什麼？"}
# 指名畫作時，分流信心不夠也可以直接改成畫作問答的意圖（不含寫入、耗時、閒聊）
NAMED_ART_INTENTS = {"art_qa", "art_search", "data_query"}
_INTRO_RE = re.compile(r"介紹|說明|講解|說說|聊聊")


def _asks_about_named(question: str) -> bool:
    """在發問、請介紹，或只說了作品名稱（「谿山行旅圖」）"""
    bare = re.sub(r"[\s？?。!！]", "", question)
    return (
        art_resolve.asks_about_it(question) or bool(_INTRO_RE.search(question)) or len(bare) <= 12
    )


def _identify_photo(image_id: str, visible: set[str], account: Account) -> dict:
    """照片是畫作還是圖紙：和以圖搜圖、問答同一個領域路由（docs/adr/007），只跑該領域的辨識。
    路由拿不準時當圖紙（機密側）；該領域沒通過驗證就是無法辨識，不改試另一個領域。
    圖紙只在目前身分看得到的圖紙裡辨識（visible，docs/adr/030）：看不到的不讀、不回名稱。"""
    found = search_service.identify_any(image_id, part_ids=visible, viewer=account)
    if found["route"]["domain"] == "art":
        art = found["artwork_result"]
        if not art["matched"]:
            return {"kind": "unknown", "id": None, "label": "知識庫中沒有這幅畫", "domain": "art"}
        a = get_store().get_artwork(art["best_artwork_id"])
        return {"kind": "art", "id": a["id"], "label": a["title"]["zh"], "domain": "art"}
    drawing = found["drawing_result"]
    if not drawing["matched"]:
        return {"kind": "unknown", "id": None, "label": "知識庫中沒有這張圖紙", "domain": "mfg"}
    p = get_store().get_part(drawing["best_part_id"])
    return {"kind": "drawing", "id": p["id"], "label": p["name"]["zh"], "domain": "mfg"}


def _names_photo(question: str, entities: list, photo: dict | None) -> bool:
    """文字只寫了照片辨識出的那一件的名稱、料號或圖號（「立式軸承座」＋同一張圖紙的照片）：
    沒有多說要做什麼，和只附照片一樣交給問答，不出澄清按鈕（TASK-20261010-001）。
    只有文字的名稱照舊要澄清（eval/route_qa.jsonl r43、r44）。"""
    kind = {"drawing": "part", "art": "artwork"}.get(photo["kind"] if photo else "")
    if not kind or not entities or any(e.kind != kind or e.id != photo["id"] for e in entities):
        return False
    rest = question
    for e in sorted(entities, key=lambda e: e.start, reverse=True):
        rest = rest[: e.start] + rest[e.end :]
    return not re.sub(r"[\s，,。？?！!、]", "", rest)


def _within_domain(result: System1Result, domain: str, cfg: dict) -> System1Result:
    """入口選了畫作或工廠（docs/adr/040）：本地分流只在那個領域的意圖裡挑，機率重新正規化。
    只影響「要交給哪個模組」，權限照樣由第 1 段依角色檢查。"""
    allowed = set(cfg["domain_intents"].get(domain, [])) | set(cfg["domain_intents"]["always"])
    kept = {k: (v if k in allowed else 0.0) for k, v in result.intent_probs.items()}
    total = sum(kept.values())
    if total <= 0:
        return result
    probs = {k: v / total for k, v in kept.items()}
    return dataclasses.replace(
        result, intent_probs=probs, detail={**(result.detail or {}), "domain": domain}
    )


def _dispatch(intent: str, question: str, entities: list, photo: dict | None) -> dict:
    """交給哪個模組、帶什麼參數（實體都在本機解析，不靠 Jev）。"""
    part = first(entities, "part")
    art = first(entities, "artwork") or first(entities, "artist")
    part_id = part.id if part else (photo["id"] if photo and photo["kind"] == "drawing" else None)
    artwork_id = art.id if art else (photo["id"] if photo and photo["kind"] == "art" else None)
    d: dict = {
        "module": intent,
        "part_id": part_id,
        "artwork_id": artwork_id,
        "question": question or DEFAULT_QUESTIONS.get(intent, ""),
    }
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
    elif intent == "batch_identify":
        d["path"] = "/batch"
    elif intent == "compare":
        d.update(_compare_targets(entities, part_id, artwork_id))
    return d


def _compare_targets(entities: list, part_id: str | None, artwork_id: str | None) -> dict:
    """並排比較的兩件（docs/adr/017）：句子裡兩件同一類的作品；只提到一件（或附照片）就先帶那一件，
    另一件在比較頁上選。"""
    store = get_store()
    found = local_router.pair(entities)
    if found:
        kind, ids = found[0], found[1][:2]
    elif part_id:
        kind, ids = "part", [part_id]
    elif artwork_id:
        kind, ids = "artwork", [artwork_id]
    else:
        return {"compare": None, "path": "/compare-items"}
    get = store.get_part if kind == "part" else store.get_artwork
    items = [x for x in (get(i) for i in ids) if x]
    labels = [x["name"]["zh"] if kind == "part" else x["title"]["zh"] for x in items]
    refs = [f"{kind}:{x['id']}" for x in items]
    query = "&".join(f"{k}={r}" for k, r in zip("ab", refs, strict=False))
    return {
        "compare": {"kind": kind, "refs": refs, "labels": labels},
        "path": "/compare-items" + (f"?{query}" if query else ""),
    }


def _target(dispatch: dict) -> dict | None:
    """已指定的對象（某張圖紙、某幅畫）與機密等級、部門：
    第 1 段檢查功能授權、產生 Metadata Filter。
    並排比較兩張圖紙：取機密等級較高的那張（兩張都要看得到）。"""
    store = get_store()
    cmp = dispatch.get("compare")
    if cmp and cmp["kind"] == "part" and cmp["refs"]:
        parts = [store.get_part(r.split(":", 1)[1]) for r in cmp["refs"]]
        p = max(parts, key=lambda x: level_rank(x["confidentiality"]))
        return {
            "id": p["id"],
            "label": p["name"]["zh"],
            "level": p["confidentiality"],
            "dept": p.get("owner", ""),
        }
    if dispatch.get("part_id") and (p := store.get_part(dispatch["part_id"])):
        return {
            "id": p["id"],
            "label": p["name"]["zh"],
            "level": p["confidentiality"],
            "dept": p.get("owner", ""),
        }
    if dispatch.get("artwork_id") and (a := store.get_artwork(dispatch["artwork_id"])):
        return {"id": a["id"], "label": a["title"]["zh"], "level": "公開", "dept": "公開"}
    return None


async def route(
    question: str,
    auth: Auth,
    request_id: str,
    image_id: str | None = None,
    forced_intent: str | None = None,
    engine: str = "auto",
    domain: str | None = None,
) -> dict:
    """auth：閘道驗證過的 JWT（帳號＋憑證內容）。engine：第 2、4～6 段由誰判斷。
    auto／jev＝用 Jev，叫不到才改地端規則；local＝只用地端規則（評估對照用）。
    engine 由 API 層決定：只有評估模式（EVAL_CONTROLS＋本機）才會傳 local 進來（docs/adr/030）。
    forced_intent（澄清按鈕）只改變交給哪個模組，不降低任何關卡：第 2 段照「使用者點的意圖」與
    「本地分流原本判斷的意圖」裡比較危險的那個判斷冒充身分。"""
    t0 = time.perf_counter()
    account = auth.account
    cfg = get_agent_config()
    # 0. 個資遮蔽：之後只看遮蔽後的文字
    question, pii = guard.mask_pii(question.strip())
    visible = visible_part_ids(account, get_store().parts)
    photo = (
        await asyncio.to_thread(_identify_photo, image_id, visible, account) if image_id else None
    )
    photo_kind = photo["kind"] if photo else None
    index = await asyncio.to_thread(get_index)
    # 看不到的圖紙和不存在一樣（docs/adr/033 F）：名稱、料號、圖號不認成實體——不代號化、
    # 不影響分流、不分派、不出現在回應（entities、mapping、dispatch、auth.filter）。
    # 不存在的名稱本來就是原文送 Jev，看不到的也一樣，回應才分不出兩者
    entities = [e for e in index.find(question) if e.kind != "part" or e.id in visible]
    masked = index.pseudonymize(question, entities)

    # 本地分流：判斷要交給哪個模組（第 1 段授權要知道要做什麼；Jev 只判斷是不是查詢）
    t1 = time.perf_counter()
    natural_risk = None
    if forced_intent:
        if forced_intent not in cfg["intents"]:
            forced_intent = "out_of_scope"
        if question:
            # 使用者點的意圖不能把風險變低：本地分流照樣判斷一次，第 2 段取兩者較危險的
            async with memory_guard.flow("route", {"bge"}):
                natural = await asyncio.to_thread(
                    local_router.classify, question, entities, photo_kind
                )
            natural_risk = cfg["intents"][natural.intent]["risk"]
        op, op_probs = local_router.classify_op(question)
        result = System1Result(
            engine="user",
            intent_probs={k: float(k == forced_intent) for k in cfg["intents"]},
            op_probs=op_probs if op else {},
            model="使用者點選",
        )
    elif photo and (not question or _names_photo(question, entities, photo)):
        # 只有照片（或文字只是照片那一件的名稱）：依本機辨識結果決定
        intent = PHOTO_INTENTS.get(photo_kind or "", "out_of_scope")
        result = System1Result(
            engine="local",
            intent_probs={k: float(k == intent) for k in cfg["intents"]},
            model="Chinese-CLIP 照片辨識",
            detail={"photo_named": bool(question)},
        )
    else:
        async with memory_guard.flow("route", {"bge"}):
            result = await asyncio.to_thread(local_router.classify, question, entities, photo_kind)
        if domain:
            # 分類只決定交給哪個模組，不能把風險變低
            # （「我是主管…改成 0」選了畫作也要照修改資料判斷）：
            # 第 2 段取「原本分流」與「限縮後」較危險的，和使用者點澄清按鈕同一個規則
            natural_risk = cfg["intents"][result.intent]["risk"]
            result = _within_domain(result, domain, cfg)
    router_ms = round((time.perf_counter() - t1) * 1000)

    decision = gate.decide(result)
    intent = result.intent
    # 知識庫目錄題（docs/adr/039）：本地分流常分錯（落到批次辨識、圖紙查找）；
    # 在第 1 段之前改成畫作問答，權限照畫作問答檢查（一般員工也能問），第 2 段照樣檢查，
    # 放行後直接列出。使用者自己點的意圖照他選的
    catalog_list = bool(
        question
        and not photo
        and not forced_intent
        and domain != "mfg"
        and catalog.is_list_question(question)
    )
    if catalog_list:
        intent = "art_qa"
        decision = dataclasses.replace(
            decision, gate="direct", options=[], reason="知識庫目錄題：直接列出知識庫的畫作"
        )
    # 指名作品的問句：本地分流常被「多高、幾、多少」拉向庫存查詢，信心不夠就請使用者選
    # （「谿山行旅圖有多高？」art_qa 0.36、data_query 0.32）。句子已經指名畫作（或畫家）、
    # 又是在發問或請介紹，就直接畫作問答：畫作不會在工廠資料庫裡。圖紙只在本來就判成圖紙問答時
    # 補信心，「L 型固定支架還剩幾件」本來就該查庫存。也在第 1 段之前，權限照改後的意圖檢查；
    # 有想略過規則的說法（「我是主管…」）時照舊請使用者選
    elif (
        decision.gate == "clarify"
        and question
        and not photo
        and not forced_intent
        and not result.flags.get("overrides_rules")
        and _asks_about_named(question)
    ):
        art = first(entities, "artwork") or first(entities, "artist")
        part = first(entities, "part")
        if art and not part and intent in NAMED_ART_INTENTS and domain != "mfg":
            intent = "art_qa"
            reason = "指名畫作的問句：直接畫作問答"
        elif part and not art and intent == "drawing_qa" and domain != "art":
            reason = "指名圖紙的問句：直接圖紙問答"
        else:
            reason = None
        if reason:
            decision = dataclasses.replace(
                decision, gate="direct", options=[], reason=f"{reason}（{decision.reason}）"
            )
    labels = {k: v["label"] for k, v in cfg["intents"].items()}
    dispatch = _dispatch(intent, question, entities, photo)
    op = result.modify_op if intent == "modify" else None
    op_label = cfg["modify_ops"].get(op or "", {}).get("label")
    if intent == "modify":
        dispatch["op"] = op
        dispatch["op_label"] = op_label

    # 1. 認證與授權：憑證已在閘道驗過；這裡用憑證的角色檢查功能與動作權限
    cmp = dispatch.get("compare")
    domain = ("mfg" if cmp["kind"] == "part" else "art") if intent == "compare" and cmp else None
    authz = guard.auth_check(
        auth.checks,
        account,
        intent,
        decision.gate,
        op,
        op_label,
        _target(dispatch),
        entities,
        domain=domain,
    )
    # 拒絕並記錄不存問句原文與文件名稱，只存雜湊（docs/adr/030）
    logged_text = (
        f"問句雜湊 {guard.fingerprint(question)}"
        if question or not photo
        else f"只有照片（{photo['kind']}）"
    )
    guard_res = None
    blocked = None
    short = None
    outcome = "pass"
    if not authz.passed:
        tag = authz.tag or "權限不符"
        no = guard.log_block(1, tag, account, logged_text, request_id, "後端硬性檢查（JWT 角色）")
        blocked = {
            "stage": 1,
            "rule": "查無資料" if authz.degraded else tag,
            "log_no": no,
            "judge": "後端硬性檢查（JWT 角色）",
            "reason": authz.reason,
            "degraded": authz.degraded,
        }
        outcome = "degraded" if authz.degraded else "blocked_auth"
    else:
        # 2. Jev 意圖路由／防護欄：正常查詢／Prompt 注入／無關閒聊
        decided = decision.gate != "clarify" and intent not in ("system", "out_of_scope")
        risk = cfg["intents"][intent]["risk"] if decided else None
        if risk and natural_risk and guard.RISK_RANK[natural_risk] > guard.RISK_RANK[risk]:
            risk = natural_risk
        # 還不確定時，地端備援照候選意圖裡最危險的那個判斷冒充身分
        # （「我是主管…改成 0」信心不夠也要擋）
        hint = max(
            (cfg["intents"][k]["risk"] for k, _ in decision.options),
            key=guard.RISK_RANK.__getitem__,
            default=None,
        )
        g0 = time.perf_counter()
        guard_res = await guard.guard_input(
            question,
            masked.text,
            masked.mapping,
            risk,
            engine != "local",
            photo_kind,
            risk_hint=hint if decision.gate == "clarify" else None,
            local_intent=intent,
        )
        guard_ms = round((time.perf_counter() - g0) * 1000)
        if not guard_res.passed:
            tag = guard_res.tag or "惡意輸入"
            judge = "雲端 Jev（只收代號化文字）" if guard_res.engine == "jev" else "地端規則"
            no = guard.log_block(2, tag, account, logged_text, request_id, judge)
            blocked = {
                "stage": 2,
                "rule": tag,
                "log_no": no,
                "judge": judge,
                "reason": guard_res.reason,
                "degraded": False,
            }
            outcome = "blocked_guard"
        elif guard_res.verdict == "chitchat":
            # 快速短路回覆：不檢索、不生成、不載入模型
            short = {
                "stage": 2,
                "by": "Jev" if guard_res.engine == "jev" else "地端",
                "kind": "chitchat",
                "reply": cfg["guard"]["chitchat_reply"],
            }
            outcome = "short_circuit"
        elif catalog_list:
            # 問答只檢索最像的幾段，列不出「全部有哪些」：直接從知識庫列出，不檢索、不呼叫 LLM
            short = {"stage": 2, "by": "地端", "kind": "catalog", "reply": catalog.reply()}
            outcome = "short_circuit"

    resolve_info, resolve_egress = None, 0
    if outcome == "pass":
        # 記憶體管理：路由結果出來就知道接下來要哪些模型，超過門檻先釋放其他的
        # bge-m3 一律保留：本地分流下一句還要用（釋放後重新載入要 1～2 秒）
        keep = set(cfg["intent_models"].get(intent, [])) | {"bge"}
        if photo and photo["kind"] == "unknown" and photo["domain"] == "art":
            keep.add("clip")  # 沒收錄的畫作：前端接著用 CLIP 做畫作卡推測（ADR 018），不先卸載
        await asyncio.to_thread(
            memory_guard.guard.check, f"智慧助理預估（{labels[intent]}）", keep, False, True
        )
        # 沒說畫名、畫家、也沒附照片的畫作問題：判斷問的是哪一幅（docs/adr/038）。
        # 放在第 2 段之後：被擋下的問句不再送 Jev；交接票也要帶推測出的畫作
        if (
            intent in ART_RESOLVE_INTENTS
            and question
            and not photo
            and not dispatch.get("artwork_id")
        ):
            guess = await art_resolve.resolve(question, masked.text, engine != "local")
            if guess is not None:
                resolved = guess.public()
                if guess.artwork_id and (a := get_store().get_artwork(guess.artwork_id)):
                    dispatch["artwork_id"] = guess.artwork_id
                    dispatch["artwork_label"] = a["title"]["zh"]
                    dispatch["artwork_guess"] = resolved
                    # 「有猴子的那幅畫裡，…陽傘是什麼顏色？」會因為「畫裡」被分成以文搜畫，
                    # 前端只列搜尋結果；
                    # 已經知道是哪幅、句子又是在發問，就改成畫作問答（「找一幅有猴子的畫」照舊搜尋）
                    # 使用者自己點了以文搜畫（forced_intent）就照他選的
                    if (
                        intent == "art_search"
                        and not forced_intent
                        and art_resolve.asks_about_it(question)
                    ):
                        intent = "art_qa"
                        dispatch["module"] = "art_qa"
                        resolved["switched_from"] = "art_search"
                resolve_info = resolved
                resolve_egress = guess.egress_bytes
        # 圖紙也一樣：「連接法蘭的圖紙是哪一版？材質是什麼？」因為「圖紙」被分成圖紙查找，
        # 前端只列零件清單、沒有回答。句子已經指名零件又是在發問，就改成圖紙問答；
        # 兩者讀同一個資料領域，問答時照樣檢查看不看得到這張圖紙。使用者自己點的照他選的
        if (
            intent == "drawing_search"
            and question
            and not forced_intent
            and dispatch.get("part_id")
            and art_resolve.asks_about_it(question)
        ):
            intent = "drawing_qa"
            dispatch["module"] = "drawing_qa"
            dispatch.pop("path", None)
        # 沒指定畫作的比較題（「哪幅畫最早完成？」）會因為「哪幅」被分成以文搜畫，前端只列搜尋結果；
        # 改成畫作問答，問答時附知識庫目錄（docs/adr/039）。使用者自己點以文搜畫時照他選的
        if intent in ART_RESOLVE_INTENTS and catalog.is_compare_question(question):
            # 句子裡有兩幅以上的畫名（「〈麥田〉和〈大碗島〉哪一幅先完成？」）：不要只鎖第一幅，
            # 改成全庫檢索＋知識庫目錄，兩幅的資料才都看得到
            named = {e.id for e in entities if e.kind == "artwork"}
            if len(named) >= 2:
                dispatch["artwork_id"] = None
                dispatch.pop("artwork_label", None)
            if intent == "art_search" and not forced_intent and not dispatch.get("artwork_id"):
                intent = "art_qa"
                dispatch["module"] = "art_qa"
            if intent == "art_qa" and not dispatch.get("artwork_id"):
                # 前端的畫作問答沒帶畫作時會改列搜尋結果；比較題要直接全庫問答
                dispatch["global_qa"] = True

    # 交接票：問答放行時，/chat 憑票沿用第 1、2 段的判斷（帳號、問句、對象都要相符，10 分鐘內有效）
    post_filter = "local" if engine == "local" else "jev"
    ticket = None
    if outcome == "pass" and intent in guard.QA_INTENTS and dispatch.get("question"):
        ticket = handoff.issue(
            account.id,
            guard.fingerprint(guard.mask_pii(dispatch["question"].strip())[0]),
            intent,
            dispatch.get("part_id") if intent == "drawing_qa" else None,
            dispatch.get("artwork_id") if intent == "art_qa" else None,
            post_filter,
            guard_res.engine if guard_res else "skip",
            request_id,
        )

    egress = (guard_res.egress_bytes if guard_res else 0) + resolve_egress
    out = {
        "request_id": request_id,
        "account": account.public(),
        "question": question,
        "pii": pii,
        "masked_text": masked.text,
        "mapping": masked.mapping,
        "entities": [
            {"kind": e.kind, "id": e.id, "label": e.label, "text": e.text} for e in entities
        ],
        "photo": photo,
        "router": {
            "engine": result.engine,
            "model": result.model,
            "latency_ms": router_ms,
            "detail": result.detail,
        },
        "intent": intent,
        "intent_label": labels[intent],
        "risk": cfg["intents"][intent]["risk"],
        "confidence": round(result.confidence, 4),
        "margin": round(result.margin, 4),
        "ranked": [
            {"intent": k, "label": labels[k], "prob": round(p, 4)} for k, p in result.ranked[:5]
        ],
        "modify_op": op,
        "flags": result.flags,
        "gate": decision.gate,
        "threshold": decision.threshold,
        "gate_reason": decision.reason,
        "options": [
            {"intent": k, "label": labels[k], "prob": round(p, 4)} for k, p in decision.options
        ],
        "auth": {**authz.public(), "token": auth.public()},
        "guard": guard_res.public() if guard_res else None,
        "outcome": outcome,
        "blocked": blocked,
        "short_circuit": short,
        "dispatch": dispatch,
        # 沒說畫名時判斷是哪一幅畫的過程（沒判斷時是 null，docs/adr/038）
        "art_resolve": resolve_info,
        # 第 4～6 段由誰判斷（伺服器決定，記在交接票裡；/chat 不收這個參數）
        "post_filter": post_filter,
        "route_ticket": ticket,
        "egress": {
            "bytes": egress,
            "to": "TypeSafe Jev" if egress else None,
            "images": 0,
        },
        "latency_ms": {
            "router": router_ms,
            "guard": guard_ms if guard_res else 0,
            "total": round((time.perf_counter() - t0) * 1000),
        },
    }
    get_logs_repo().add_route_log(
        {
            "request_id": request_id,
            "created_at": get_logs_repo().now(),
            "account_id": account.id,
            "question": question,
            "masked_text": masked.text,
            "has_photo": int(bool(image_id)),
            "engine": guard_res.engine if guard_res else "none",
            "model": result.model,
            "fallback_reason": guard_res.fallback_reason if guard_res else None,
            "intent": intent,
            "modify_op": op,
            "confidence": out["confidence"],
            "margin": out["margin"],
            "gate": decision.gate,
            "overrides_rules": int(
                bool(result.flags.get("overrides_rules") or (guard_res and guard_res.overrides))
            ),
            "egress_bytes": egress,
            "system1_ms": router_ms,
            "total_ms": out["latency_ms"]["total"],
            "outcome": outcome,
        }
    )
    log.info(
        "route",
        extra={
            "fields": {
                "request_id": request_id,
                "intent": intent,
                "confidence": out["confidence"],
                "gate": decision.gate,
                "outcome": outcome,
                "guard": guard_res.engine if guard_res else None,
                "egress": out["egress"],
            }
        },
    )
    return out
