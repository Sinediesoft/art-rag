"""七段權限控管（docs/adr/015）：使用者提問從收到到生成的七道關卡。

這裡是第 1、2、4、5、6 段與「拒絕並記錄」。

個資遮蔽：一收到就把手機、Email、身分證換成 [電話1]…，原值不保留；
之後各段、紀錄、送 Jev 都只看遮蔽後的文字
1. 認證與授權：API 閘道驗 JWT 的簽章與效期（identity.gateway，不符就 401、碰不到任何模型）；
   這裡再用憑證裡的角色檢查「要做的事」能不能做（auth_check），同時產生第 3 段的 Metadata Filter。
   文件層級的權限不在這裡透露，交給第 3 段過濾、第 6 段降級
2. Jev 意圖路由／防護欄（Jev Choice）：正常查詢／Prompt 注入／無關閒聊三選一，只收代號化文字；
   注入就拒絕並記錄、閒聊就快速短路回覆。叫不到 Jev（斷網、逾時、回錯誤、沒金鑰）才改用地端規則
3. 權限感知檢索：chat_service.retrieve，Metadata Filter 只照 JWT 產生（clearance＋dept）
4. Jev Noul 雙重驗證：每段 ① is_relevant ② security_leak_check，任一不通過就剔除
5. Jev Score 評分重排：通過的段落依幫助程度打分（0～3），取前 max_keep 段
6. 生成閘門：Jev Noul 檢查可答性與合規；權限內沒有可答內容就降級回「查無資料」，
   不透露「有文件但你沒有權限」
7. 本地 LLM 生成：只依第 5 段留下的段落回答（chat_service）

第 4～6 段只把公開段落（畫作）代號化後送 Jev；內部、機密段落（工廠圖紙）不出廠，
改在地端判斷（規則掃描＋本地 Qwen3-VL 挑選＋相似度與關鍵詞）。
規則與 Jev 的題目都在 shared/agent.yaml 的 guard；資料範圍在 shared/access.yaml。

2026-10-06 修補（docs/adr/030）：
- 地端規則比對前先正規化（NFKC、去零寬字元、小寫，另比一次去掉空白標點的版本）
- 第 2 段的確定性 hard-block 永遠先跑，Jev 只能增加攔截，不能覆寫
- Jev 回應缺欄、不是數字、NaN、無限大、超出範圍一律當「叫不到 Jev」，改用地端判斷（不會放行或 500）
- 第 5 段一律套最低分數與最多 3 段；第 6 段的地端閘門實際檢查可答性與合規，不因文件卡或圖面自動放行
- 第 7 段生成後的輸出檢查（check_output）
- 拒絕並記錄只存事件類型、文件／段落 ID 或雜湊，不存問句與段落原文
"""

import asyncio
import hashlib
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from functools import lru_cache

import httpx

from app.agent import jev
from app.agent.entities import Entity, get_index
from app.core.config import get_agent_config, get_settings
from app.rag import rearrange as rearrange_mod
from app.rag.prompt import load_template
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.logs_repo import get_logs_repo
from app.services.identity import (
    Account,
    accounts,
    can_view_part,
    domain_label,
    level_rank,
    scope_allows,
)

# 每個意圖會讀哪一個資料領域（None＝不碰地端資料）
INTENT_DOMAIN = {
    "art_search": "art",
    "art_qa": "art",
    "drawing_search": "mfg",
    "drawing_qa": "mfg",
    "reconstruct": "mfg",
    "data_query": "factory",
    "schedule": "factory",
    "modify": "factory",
    # 並排比較：兩張圖紙是 mfg、兩幅畫是 art（agent_service 依句子裡的作品傳 domain 進來）
    "compare": "art",
}
# 問答：文件看不看得到交給第 3 段 Metadata Filter 與第 6 段生成閘門（不在第 1 段透露）
QA_INTENTS = {"art_qa", "drawing_qa"}
RISK_RANK = {"out_of_scope": 0, "read": 1, "heavy": 2, "write": 3}
RISK_VERB = {"read": "查詢", "heavy": "執行耗時工作", "write": "修改資料"}
VERDICT_LABEL = {"query": "正常查詢", "attack": "Prompt 注入或越權", "chitchat": "無關閒聊"}
# 第 4～6 段一次問多段，給 Jev 長一點的時間（第 2 段用 .env 的 JEV_TIMEOUT_S）
POST_TIMEOUT_S = 4.0
# 第 5 段放進上下文的段落上限：agent.yaml 的 max_keep 再大也不超過這個數（docs/adr/015、019）
MAX_CONTEXT = 3
STAGE_NAMES = {
    1: "認證與授權",
    2: "Jev Choice",
    3: "Metadata Filter",
    4: "Jev Noul 雙重驗證",
    5: "Jev Score 評分重排",
    6: "生成閘門",
    7: "本地 LLM 生成",
}


@dataclass
class Check:
    key: str
    label: str
    by: str  # 地端／Jev
    ok: bool | None  # None＝沒判定（等使用者選、或不放進上下文）
    detail: str
    warn: bool = False


def _checks(items: list[Check]) -> list[dict]:
    return [asdict(c) for c in items]


@lru_cache
def _rules() -> dict:
    g = get_agent_config()["guard"]
    lr = g["local_rules"]
    out = g["output"]
    lg = g["local_gate"]
    return {
        "pii": [(r["kind"], r["code"], re.compile(r["pattern"])) for r in g["pii"]],
        "direct": [(r["rule"], re.compile(r["pattern"], re.I)) for r in lr["direct"]],
        "suspect": [(r["rule"], re.compile(r["pattern"], re.I)) for r in lr["suspect"]],
        "indirect": re.compile(lr["indirect"], re.I),
        "leak": re.compile(lr["leak"], re.I),
        "sensitive": re.compile(lr["sensitive"], re.I),
        "directive": re.compile(lr["directive"], re.I),
        "wrapper": re.compile(lr["wrapper"], re.I),
        "ai_target": re.compile(lr["ai_target"], re.I),
        "credential": re.compile(out["credential"], re.I),
        "sensitive_number": re.compile(out["sensitive_number"], re.I),
        "overview": re.compile(lg["overview"], re.I),
        # 長的先換掉：「有哪些」要比「有」先處理
        "stopwords": sorted(lg["stopwords"], key=len, reverse=True),
    }


def _cfg() -> dict:
    return get_agent_config()["guard"]


# ================================================================ 正規化（地端規則比對前）
# 零寬、方向控制等看不見的字元：拿來把「成本」拆成「成​本」躲過規則
_INVISIBLE = re.compile(r"[­᠎​-‏‪-‮⁠-⁤﻿]")
# 去掉空白與標點的版本：擋「內 部-成·本」這種拆字
_SEPARATORS = re.compile(
    r"[\s\-_.·・•*~|/\\、,，。:：;；'\"「」『』()（）\[\]【】<>《》〈〉!！?？+=#@&%^`…]+"
)


def normalize(text: str) -> str:
    """NFKC（全形英數、相容字元變一般寫法）、去掉看不見的字元、轉小寫。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = _INVISIBLE.sub("", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    return t.lower()


def compact(text: str) -> str:
    return _SEPARATORS.sub("", normalize(text))


def has_hidden_chars(text: str) -> bool:
    return bool(_INVISIBLE.search(text or "")) or any(
        unicodedata.category(ch) == "Cf" for ch in text or ""
    )


def _find(rx: re.Pattern, text: str) -> re.Match | None:
    """正規化後比一次、去掉空白標點後再比一次。"""
    return rx.search(normalize(text)) or rx.search(compact(text))


def fingerprint(text: str) -> str:
    """不可逆的短雜湊：拒絕並記錄、稽核只存這個，不存原文（docs/adr/030）。"""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:12]


def terms(text: str) -> set[str]:
    """關鍵詞：去掉疑問詞與虛詞後，中文連續字兩兩一組、英數詞整個。
    第 4 段的地端相關性、第 6 段的地端可答性用。"""
    t = normalize(text)
    for w in _rules()["stopwords"]:
        t = t.replace(w, " ")
    out: set[str] = set()
    for run in re.findall(r"[㐀-鿿]+", t):
        out |= {run[i : i + 2] for i in range(len(run) - 1)}
    out |= {w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", t)}
    return out


def _passage_terms(s: dict) -> set[str]:
    """段落的關鍵詞：標題（作品／圖紙名稱）＋主題＋內文。段落很少重複自己是哪件作品，
    問「步進馬達安裝板的公差」時名稱要算在段落裡。"""
    return terms(f"{s.get('title', '')} {s['topic']} {s['text']}")


def coverage(question_terms: set[str], text_terms: set[str]) -> float:
    if not question_terms:
        return 0.0
    return len(question_terms & text_terms) / len(question_terms)


# ================================================================ 個資遮蔽
def mask_pii(text: str) -> tuple[str, list[dict]]:
    """手機、市話、Email、身分證換成 [電話1]、[Email1]…；只回傳種類與代號，不留原值。"""
    found: list[dict] = []
    counters: dict[str, int] = {}
    for kind, code, rx in _rules()["pii"]:

        def sub(m: re.Match, kind: str = kind, code: str = code) -> str:
            counters[code] = counters.get(code, 0) + 1
            c = f"[{code}{counters[code]}]"
            found.append({"kind": kind, "code": c})
            return c

        text = rx.sub(sub, text)
    return text, found


# ================================================================ 第 1 段：認證與授權
@dataclass
class MetaFilter:
    """第 3 段向量檢索的 Metadata Filter：只照 JWT 的 clearance 與 depts 產生，
    不採用使用者輸入或模型產出的條件（前端改不了）。"""

    domain: str
    clearance: int
    depts: list[str]
    doc_id: str | None = None
    doc_label: str | None = None
    doc_level: str | None = None

    @classmethod
    def of(cls, account: Account, domain: str, doc: dict | None = None) -> "MetaFilter":
        f = cls(domain, account.clearance, ["公開", *account.depts])
        if doc:
            f.doc_id, f.doc_label = doc["id"], doc["label"]
            # 看不到的文件不回傳它的機密等級：不透露「有文件但你沒有權限」
            if f.allows(doc["level"], doc.get("dept", "公開")):
                f.doc_level = doc["level"]
        return f

    def allows(self, level: str, dept: str) -> bool:
        # 和 identity.can_view_part 同一條規則（docs/adr/030）
        return scope_allows(self.clearance, self.depts, level, dept)

    @property
    def levels(self) -> list[str]:
        order = ["公開", "內部", "機密"]
        return [x for x in order if level_rank(x) <= self.clearance]

    @property
    def text(self) -> str:
        depts = ", ".join(f'"{x}"' for x in self.depts)
        doc = f' AND doc_id = "{self.doc_id}"' if self.doc_id else ""
        return (
            f'domain = "{domain_label(self.domain)}" AND clearance <= {self.clearance}'
            f" AND dept IN ({depts}){doc}"
        )

    def public(self) -> dict:
        return {
            **asdict(self),
            "levels": self.levels,
            "domain_label": domain_label(self.domain),
            "text": self.text,
        }


def visible(account: Account, doc: dict) -> bool:
    """這張圖紙在不在憑證的權限內：和 identity.can_view_part 同一個函式（領域＋機密等級＋部門）。"""
    return can_view_part(account, {"confidentiality": doc["level"], "owner": doc.get("dept", "")})


@dataclass
class AuthResult:
    passed: bool
    pending: bool
    checks: list[Check]
    tag: str | None = None
    reason: str | None = None
    # 換成誰就可以（給「切換成〇〇再試一次」）；文件層級的降級不給
    retry: dict | None = None
    filter: MetaFilter | None = None
    # True＝不是權限不足，而是「查無資料」的降級回應（不透露有文件但你沒有權限）
    degraded: bool = False

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "pending": self.pending,
            "checks": _checks(self.checks),
            "tag": self.tag,
            "reason": self.reason,
            "retry": self.retry,
            "filter": self.filter.public() if self.filter else None,
            "degraded": self.degraded,
        }


def needed_op(intent: str, op: str | None) -> str | None:
    """這個意圖要哪一項操作權限：排程要 schedule_run，修改要該項操作，其他（查詢、3D）人人可用。"""
    if intent == "schedule":
        return "schedule_run"
    if intent == "modify":
        return op
    return None


def _who_can(
    domain: str, level: str | None, op: str | None, entities: list[Entity]
) -> list[Account]:
    """誰能做：領域、等級、操作都符合；句子提到的倉庫或客戶在誰的範圍，誰排前面。"""
    wh = {e.id for e in entities if e.kind == "warehouse"}
    cust = {e.id for e in entities if e.kind == "customer"}
    ok = [
        a
        for a in accounts().values()
        if a.can_read(domain) and (level is None or a.can_see(level)) and (op is None or a.can(op))
    ]
    return sorted(ok, key=lambda a: not (wh & set(a.warehouses) or cust & set(a.customers)))


def auth_check(
    token_checks: list[dict],
    account: Account,
    intent: str,
    gate: str,
    op: str | None,
    op_label: str | None,
    doc: dict | None,
    entities: list[Entity],
    domain: str | None = None,
) -> AuthResult:
    """token_checks：閘道驗過的簽章、效期、角色（identity.verify）。
    doc：已指定的對象 {id, label, level, dept}（某幅畫、某張圖紙），沒有就是 None；
    並排比較兩張圖紙時是兩張裡機密等級較高的那張。domain：覆寫意圖預設的資料領域。"""
    checks = [Check(c["key"], c["label"], "地端", c["ok"], c["detail"]) for c in token_checks]
    if gate == "clarify":
        checks.append(
            Check("function", "功能授權", "地端", None, "要做什麼還不確定，等你選擇後再檢查")
        )
        return AuthResult(True, True, checks)
    domain = domain or INTENT_DOMAIN.get(intent)
    if domain is None:
        detail = (
            "打開批次辨識頁：每張照片看得到什麼，由辨識 API 依你的資料範圍決定"
            if intent == "batch_identify"
            else "不涉及地端資料，直接回覆"
        )
        checks.append(Check("function", "功能授權", "地端", True, detail))
        return AuthResult(True, False, checks)

    tag = None
    degraded = False
    dlabel = domain_label(domain)
    filt = MetaFilter.of(account, domain, doc) if domain in ("art", "mfg") else None
    # 功能授權：角色能不能用這個資料領域（訪客不能用工廠圖紙、工廠資料庫）
    if not account.can_read(domain):
        checks.append(
            Check(
                "function",
                "功能授權",
                "地端",
                False,
                f"roles={[account.role_label]} 不能使用「{dlabel}」（{account.scope_note}）",
            )
        )
        tag = "權限不足"
    elif domain == "mfg" and doc and intent not in QA_INTENTS and not visible(account, doc):
        # 3D、圖紙查找指定了看不到的圖紙：降級成「查無資料」，不透露這張圖紙存在
        # （庫存、訂單、工單是工廠資料庫的權限，和圖紙的機密等級無關）
        checks.append(Check("function", "功能授權", "地端", False, _cfg()["degrade_message"]))
        tag, degraded = "資料範圍不符", True
    else:
        detail = {
            "factory": "可查工廠資料庫（唯讀連線）",
            "art": "可查公開的畫作知識庫",
        }.get(domain, f"可使用「{dlabel}」；看得到哪些文件由第 3 段 Metadata Filter 決定")
        checks.append(Check("function", "功能授權", "地端", True, detail))

    # 動作權限：查詢、3D 人人可用；排程、修改看角色
    need = needed_op(intent, op)
    any_write = any(o.startswith(("stock_", "so_", "wo_")) for o in account.ops)
    if intent == "modify" and (op is None or op == "other"):
        if op == "other":
            checks.append(
                Check(
                    "action",
                    "動作權限",
                    "地端",
                    False,
                    "零件主檔、機台資料等不開放用對話修改（任何身分都不行）",
                )
            )
            tag = tag or "權限不足"
        elif not any_write:
            checks.append(
                Check(
                    "action",
                    "動作權限",
                    "地端",
                    False,
                    f"〈{account.label}〉沒有修改資料的權限"
                    + ("（訪客只能查詢）" if account.role == "guest" else ""),
                )
            )
            tag = tag or "權限不足"
        else:
            checks.append(
                Check(
                    "action",
                    "動作權限",
                    "地端",
                    True,
                    "可以修改部分資料；是哪一種操作要等參數抽取後再判定",
                    warn=True,
                )
            )
    elif need is None:
        detail = "3D 重建所有身分都可以" if intent == "reconstruct" else "查詢"
        checks.append(Check("action", "動作權限", "地端", True, detail))
    elif account.can(need):
        what = "執行排程" if intent == "schedule" else f"做「{op_label or need}」"
        checks.append(Check("action", "動作權限", "地端", True, f"{account.role_label}可以{what}"))
    else:
        what = "執行排程" if intent == "schedule" else f"「{op_label or need}」"
        roles = "、".join(dict.fromkeys(a.role_label for a in _who_can(domain, None, need, [])))
        checks.append(
            Check(
                "action",
                "動作權限",
                "地端",
                False,
                f"訪客只能查詢，不能{'執行排程' if intent == 'schedule' else '修改資料'}"
                if account.role == "guest"
                else f"〈{account.label}〉沒有{what}的權限（{roles or '沒有任何身分'}才可以）",
            )
        )
        tag = tag or "權限不足"

    failed = [c for c in checks if c.ok is False]
    retry = None
    if failed and op != "other" and not degraded:
        who = _who_can(domain, None, need, entities)
        if who:
            retry = {"account_id": who[0].id, "label": who[0].label}
    return AuthResult(
        not failed,
        False,
        checks,
        tag,
        "；".join(dict.fromkeys(c.detail for c in failed)) or None,
        retry,
        filt,
        degraded,
    )


# ================================================================ Jev 呼叫的紀錄（給畫面看）
def _call_info(
    stage: int, reply: jev.JevReply, sent: list[str], mapping: dict, answers: list[dict]
) -> dict:
    return {
        "stage": stage,
        "model": reply.model,
        "latency_ms": reply.latency_ms,
        "bytes": reply.bytes,
        "request": reply.request,
        "sent": sent,
        "mapping": mapping,
        "answers": answers,
    }


# ================================================================ 第 2 段：Jev 意圖路由／防護欄
@dataclass
class GuardResult:
    passed: bool
    engine: str  # jev／local／skip
    checks: list[Check] = field(default_factory=list)
    tag: str | None = None
    reason: str | None = None
    skipped: str | None = None
    fallback_reason: str | None = None
    call: dict | None = None
    # Jev Choice 的結論：query（正常查詢）／attack（注入）／chitchat（閒聊 → 快速短路回覆）
    verdict: str = "query"
    # 給路由紀錄：有沒有冒充身分或要求略過管控的說法
    overrides: bool = False

    @property
    def egress_bytes(self) -> int:
        return self.call["bytes"] if self.call else 0

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "engine": self.engine,
            "verdict": self.verdict,
            "checks": _checks(self.checks),
            "tag": self.tag,
            "reason": self.reason,
            "skipped": self.skipped,
            "fallback_reason": self.fallback_reason,
            "call": self.call,
        }


def _local_hits(text: str) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    r = _rules()
    direct = next(((rule, m.group(0)) for rule, rx in r["direct"] if (m := _find(rx, text))), None)
    suspects = [(rule, m.group(0)) for rule, rx in r["suspect"] if (m := _find(rx, text))]
    return direct, suspects


async def local_llm_verdict(text: str) -> tuple[str | None, str, int]:
    """地端模型備援（docs/adr/023）：叫不到 Jev、地端規則也沒命中時，
    請本地 Qwen3-VL 判斷「攻擊／正常」。

    回傳（attack／query／None, 說明, 毫秒）。None＝沒啟用、mock、逾時、連不上或輸出看不懂，
    這時只用地端規則的結果。只連本地主推論伺服器（hybrid），不外送。"""
    cfg = _cfg().get("local_llm") or {}
    if not cfg.get("enabled") or get_settings().llm_mode == "mock":
        return None, "未啟用", 0
    tpl = load_template(cfg["prompt_version"])
    messages = [
        {"role": "system", "content": tpl["system"]},
        {
            "role": "user",
            "content": [{"type": "text", "text": tpl["user"].replace("{{message}}", text)}],
        },
    ]
    t0 = time.perf_counter()

    async def judge() -> str:
        provider = get_provider("hybrid")
        provider.max_tokens, provider.temperature = int(cfg.get("max_tokens", 4)), 0
        return "".join([p async for p in provider.stream(messages)])

    try:
        out = await asyncio.wait_for(judge(), float(cfg.get("timeout_s", 4)))
    except TimeoutError:
        return (
            None,
            f"地端模型逾時（>{cfg.get('timeout_s', 4)} 秒）",
            round((time.perf_counter() - t0) * 1000),
        )
    except (ProviderUnavailable, httpx.HTTPError) as e:
        return (
            None,
            f"地端模型無法使用：{type(e).__name__}",
            round((time.perf_counter() - t0) * 1000),
        )
    ms = round((time.perf_counter() - t0) * 1000)
    if "攻擊" in out:
        return "attack", "地端模型判為攻擊", ms
    if "正常" in out:
        return "query", "地端模型判為正常", ms
    return None, f"地端模型輸出看不懂：{out.strip()[:20]}", ms


def _input_request(pseudo: str, photo_kind: str | None) -> tuple[dict, dict]:
    g = _cfg()
    state: dict = {"context": g["input_context"], "user_message": pseudo}
    if photo_kind:
        # 照片本身不送：只告訴 Jev 本機辨識出照片是畫作還是圖紙
        state["attached_photo"] = {"art": "畫作照片", "drawing": "工廠圖紙照片"}.get(
            photo_kind, "無法辨識的照片"
        )
    questions = {
        "intent_guard": {
            "type": "choice",
            "instructions": g["intent_question"],
            "criteria": g["intent_classes"],
        }
    }
    return state, questions


async def guard_input(
    text: str,
    pseudo: str,
    mapping: dict,
    risk: str | None,
    use_jev: bool,
    photo_kind: str | None = None,
    risk_hint: str | None = None,
    local_intent: str | None = None,
) -> GuardResult:
    """text：遮蔽個資後的原句（地端規則看這個）；pseudo：再代號化後的句子（只有這個送 Jev）。
    risk：本地分流判斷的動作類別（read／heavy／write）；還不確定時為 None，地端備援改用 risk_hint
    （候選意圖裡最危險的那個）判斷冒充身分。local_intent：本地分流的意圖（地端備援判斷閒聊用）。"""
    if not text:
        return GuardResult(True, "skip", skipped="只有照片，沒有文字要判斷")
    th = float(_cfg()["jev_threshold"])
    checks: list[Check] = []
    tag = None
    fallback = None
    reply = None
    # 確定性的 hard-block 永遠先跑（docs/adr/030）：命中就擋，Jev 不能覆寫，也不必再送 Jev
    direct, suspects = _local_hits(text)
    eff = risk or risk_hint or "read"
    said = "、".join(f"「{m}」" for _, m in suspects)
    if direct or (suspects and eff in ("heavy", "write")):
        if direct:
            detail = f"命中「{direct[0]}」（「{direct[1]}」）→ 攔截（地端硬性規則，Jev 不能覆寫）"
            tag = direct[0]
        else:
            detail = (
                f"{'＋'.join(r for r, _ in suspects)}（{said}），又要{RISK_VERB[eff]} → 越權嘗試"
                "（地端硬性規則，Jev 不能覆寫）"
            )
            tag = "越權嘗試"
        return GuardResult(
            passed=False,
            engine="local",
            checks=[Check("intent_guard", "意圖防護", "地端", False, detail)],
            tag=tag,
            reason=detail,
            verdict="attack",
            overrides=True,
        )

    if use_jev:
        state, questions = _input_request(pseudo, photo_kind)
        try:
            reply = await jev.ask(state, questions)
        except jev.JevUnavailable as e:
            fallback = str(e)

    keys = list(VERDICT_LABEL)
    if reply is not None:
        try:
            best, _, probs = reply.choice("intent_guard", keys)
        except jev.JevUnavailable as e:
            # 缺欄、不是數字、NaN、超出範圍：當作叫不到 Jev，改用地端規則（不放行、不 500）
            reply, fallback = None, str(e)

    if reply is not None:
        # Choice 回的是校準過的機率：注入、閒聊要 ≥ 門檻才採信；都不到就當正常查詢（只提醒）
        verdict = (
            "attack"
            if probs["attack"] >= th
            else "chitchat"
            if probs["chitchat"] >= th
            else "query"
        )
        p = probs[verdict]
        if verdict == "attack":
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "Jev",
                    False,
                    f"Jev Choice 判為「Prompt 注入或越權」（{p:.2f}）→ 攔截，不進檢索",
                )
            )
            tag = "提示詞注入"
        elif verdict == "chitchat":
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "Jev",
                    True,
                    f"Jev Choice 判為「無關閒聊」（{p:.2f}）→ 快速短路回覆，不檢索、不生成",
                    warn=True,
                )
            )
        else:
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "Jev",
                    True,
                    f"Jev Choice 判為「正常查詢」（{p:.2f}）"
                    + ("" if best == "query" else f"；最高的是{VERDICT_LABEL[best]}但不到門檻"),
                    warn=best != "query",
                )
            )
        call = _call_info(
            2,
            reply,
            [pseudo],
            mapping,
            [
                {
                    "label": f"intent_guard・{VERDICT_LABEL[k]}",
                    "value": f"{probs[k]:.2f}",
                    "alert": k == verdict and k != "query",
                }
                for k in keys
            ],
        )
        engine = "jev"
        overrides_flag = verdict == "attack" or bool(suspects)
    else:
        # 叫不到 Jev 時的備援：hard-block 上面已經跑過；規則未命中時再問本地模型
        call = None
        verdict = "query"
        llm = await local_llm_verdict(text)
        if llm[0] == "attack":
            # 規則認不出的語意式說法：本地 Qwen3-VL 再判斷一次（docs/adr/023）
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",  # API 的 by 只有地端／Jev；是本地模型判斷的寫在說明裡
                    False,
                    f"地端規則沒命中，本地 Qwen3-VL 判為「Prompt 注入或越權」（{llm[2]} ms）→ 攔截",
                )
            )
            tag, verdict = "提示詞注入（地端模型）", "attack"
        elif local_intent == "out_of_scope":
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",
                    True,
                    "本地分流判為超出範圍（閒聊）→ 快速短路回覆",
                    warn=True,
                )
            )
            verdict = "chitchat"
        else:
            note = f"；有{said}的說法，但只是查詢，身分以第 1 段的憑證為準" if suspects else ""
            llm_note = (
                f"；{llm[1]}（{llm[2]} ms）"
                if llm[0] == "query"
                else "（換句話說的攻擊認不出來）"
                if llm[1] == "未啟用"
                else f"；{llm[1]}，只用地端規則"
            )
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",
                    True,
                    "沒有命中已知的注入樣式" + llm_note + note,
                    warn=bool(suspects),
                )
            )
        engine = "local"
        overrides_flag = bool(suspects)

    failed = [c for c in checks if c.ok is False]
    return GuardResult(
        passed=not failed,
        engine=engine,
        checks=checks,
        tag=tag,
        reason="；".join(c.detail for c in failed) or None,
        fallback_reason=fallback,
        call=call,
        verdict=verdict,
        overrides=overrides_flag,
    )


# ================================================================ 第 4～6 段：驗證、重排、生成閘門
@dataclass
class StageInfo:
    engine: str  # jev／local
    checks: list[Check] = field(default_factory=list)
    call: dict | None = None
    fallback_reason: str | None = None
    ms: int = 0

    def public(self) -> dict:
        return {
            "engine": self.engine,
            "checks": _checks(self.checks),
            "call": self.call,
            "fallback_reason": self.fallback_reason,
            "ms": self.ms,
        }


@dataclass
class GateInfo(StageInfo):
    passed: bool = True
    message: str | None = None

    def public(self) -> dict:
        return {**super().public(), "passed": self.passed, "message": self.message}


@dataclass
class PostResult:
    mode: str  # jev／local：第 4～6 段由誰判斷（2026-10-06 起沒有只掃描的 scan，docs/adr/030）
    kept: list[dict]
    candidates: int
    verify: StageInfo
    rerank: StageInfo | None = None
    gate: GateInfo | None = None
    # 第 4 段 security_leak_check 剔除的段落（chunk_id → Jev／地端）
    flagged: list[dict] = field(default_factory=list)
    flagged_by: dict[str, str] = field(default_factory=dict)
    # 與提問無關（第 4 段）、分數太低或超過上限（第 5 段）沒放進上下文的段落
    dropped: list[dict] = field(default_factory=list)
    cloud: int = 0  # 送 Jev 的段落數
    local: int = 0  # 留在地端判斷的段落數
    rearrange: dict | None = None
    ms: int = 0

    @property
    def calls(self) -> list[dict]:
        stages = [self.verify, self.rerank, self.gate]
        return [s.call for s in stages if s and s.call]

    @property
    def egress_bytes(self) -> int:
        return sum(c["bytes"] for c in self.calls)

    @property
    def degraded(self) -> bool:
        return self.gate is not None and not self.gate.passed

    def caught_by(self, chunk_id: str) -> str:
        """這段的洩密風險是誰抓到的：Jev 或地端規則。"""
        return self.flagged_by.get(chunk_id, "地端")

    def public(self) -> dict:
        def brief(s: dict) -> dict:
            return {"chunk_id": s["chunk_id"], "title": s["title"], "topic": s["topic"]}

        return {
            "mode": self.mode,
            "engine": "jev" if self.calls else "local",
            "candidates": self.candidates,
            "kept": len(self.kept),
            "flagged": [{**brief(s), "by": self.caught_by(s["chunk_id"])} for s in self.flagged],
            "dropped": [brief(s) for s in self.dropped],
            "cloud": self.cloud,
            "local": self.local,
            "verify": self.verify.public(),
            "rerank": self.rerank.public() if self.rerank else None,
            "gate": self.gate.public() if self.gate else None,
            "rearrange": self.rearrange,
            "egress_bytes": self.egress_bytes,
            "ms": self.ms,
        }


def _head(s: dict) -> str:
    return f"〈{s['title']}〉{s['topic']}："


def local_leak(text: str, level: str = "公開") -> str | None:
    """地端的 security_leak_check：段落有洩密風險就回原因。
    比對前先正規化（擋全形、零寬字元、拆字）。
    level 不是「公開」（內部、機密段落，不送 Jev）時更嚴：無法安全判斷就剔除（fail closed）。
    兩件並排比較的差異摘要也用（docs/adr/017）。"""
    r = _rules()
    if _find(r["indirect"], text):
        return "夾帶要 AI 執行的指令"
    if _find(r["leak"], text):
        return "要求附上內部資料或含帳密"
    sensitive = _find(r["sensitive"], text)
    if sensitive and _find(r["directive"], text):
        return "誘導把內部資料寫進回覆"
    if _find(r["wrapper"], text) and (sensitive or level != "公開"):
        return "以檢核／除錯需求包裝的資料外洩指令"
    if level != "公開":
        if has_hidden_chars(text):
            return "含看不見的字元，無法安全判斷"
        if _find(r["directive"], text) and _find(r["ai_target"], text):
            return "對 AI 下指令，無法安全判斷"
    return None


async def _ask(state: dict, questions: dict) -> tuple[jev.JevReply | None, str | None]:
    try:
        return await jev.ask(state, questions, timeout_s=POST_TIMEOUT_S), None
    except jev.JevUnavailable as e:
        return None, str(e)


def local_answerable(question: str, kept: list[dict], card: dict | None) -> tuple[bool, str]:
    """第 6 段地端的 answerable：問題的關鍵詞有沒有出現在留下的段落與作品資料裡。
    不因為有文件卡、圖面或任一段高相似度就放行（docs/adr/030）。"""
    lg = _cfg()["local_gate"]
    q = terms(question)
    if not q:
        if _find(_rules()["overview"], question) and card and kept:
            return True, f"概覽問題：已指定文件，{len(kept)} 段通過第 4、5 段"
        return False, "問題沒有可以比對的關鍵詞，無法確認現有資料答得出來"
    ctx: set[str] = set()
    for s in kept:
        ctx |= _passage_terms(s)
    if card:
        labels = " ".join(lg["card_labels"].get(card.get("domain", "art"), []))
        ctx |= terms(f"{card['text']} {labels}")
    cov = coverage(q, ctx)
    need = float(lg["min_coverage"])
    where = f"{len(kept)} 段參考資料" + ("與作品資料" if card else "")
    return (
        cov >= need,
        f"問題關鍵詞 {len(q & ctx)}/{len(q)} 出現在{where}裡（覆蓋率 {cov:.2f}，門檻 {need:g}）",
    )


def local_compliant(question: str, kept: list[dict]) -> tuple[bool, str]:
    """第 6 段地端的 compliant：問題本身不是在要內部資料，留下的段落沒有帳密、個資。"""
    r = _rules()
    if m := _find(r["sensitive"], question):
        return False, f"問題在要內部資料（「{m.group(0)}」）"
    for s in kept:
        if _find(r["credential"], s["text"]) or any(rx.search(s["text"]) for _, _, rx in r["pii"]):
            return False, f"{_head(s)}含帳密或個資"
    return True, "問題沒有要內部資料，留下的段落沒有帳密、個資"


async def process_passages(
    question: str,
    sources: list[dict],
    mode: str,
    strategy: str = "hybrid",
    card: dict | None = None,
    rearrange: bool | None = None,
) -> PostResult:
    """sources：第 3 段檢索出的候選段落（每段有 level）。回傳留下的段落（ref 重新編號 1..n）。
    rearrange：地端段落要不要由本地 Qwen3-VL 挑（MIRA 的 Rearrange；None＝伺服器設定）。
    開或關都只換「誰判斷相關性」，洩密規則、最低分數、最多 3 段與生成閘門照樣執行。

    mode＝jev：公開段落代號化後送 Jev（叫不到、或回應缺欄／格式不對就改地端判斷）；
    mode＝local：全部在地端判斷。兩種都完整跑第 4～6 段：雙重驗證 → 評分重排（最低分數、最多 3 段）
    → 生成閘門（docs/adr/015、019）。沒有「只掃描」的模式：誰呼叫都不能略過第 5、6 段。
    card：已指定且看得到的作品／圖紙資料 {level, text, domain}
    （生成時也會放進 prompt，閘門一起看）。"""
    if mode not in ("jev", "local"):
        raise ValueError(f"第 4～6 段只有 jev／local 兩種：{mode}")
    t0 = time.perf_counter()
    g = _cfg()
    th = float(g["jev_threshold"])
    n = len(sources)
    pid = {s["chunk_id"]: f"p{i + 1}" for i, s in enumerate(sources)}
    # 沒標等級的段落當機密（預設不允許）
    leak_by_rule = {s["chunk_id"]: local_leak(s["text"], s.get("level", "機密")) for s in sources}
    use_jev = mode == "jev" and jev.status()[0]
    public = [s for s in sources if s.get("level") == "公開"] if mode == "jev" else []

    # 代號化一次，第 4～6 段共用同一套代號（只代號化會送 Jev 的公開內容）
    pseudo: dict[str, str] = {}
    mapping: dict = {}
    pq = mask_pii(question)[0]
    card_public = bool(card) and card.get("level") == "公開"
    if public or (mode == "jev" and card_public):
        idx = get_index()
        texts = [pq, *(mask_pii(s["text"])[0] for s in public)]
        if card_public:
            texts.append(mask_pii(card["text"])[0])
        out, mapping = idx.pseudonymize_many(texts)
        pq = out[0]
        pseudo = {s["chunk_id"]: t for s, t in zip(public, out[1 : 1 + len(public)], strict=True)}
        if card_public:
            pseudo["__card__"] = out[-1]

    # ---------------------------------------------------------------- 第 4 段：Jev Noul 雙重驗證
    t4 = time.perf_counter()
    verify = StageInfo("local")
    jev_leak: dict[str, float] = {}
    jev_rel: dict[str, float] = {}
    if public:
        state = {
            "context": g["passage_context"],
            "question": pq,
            "passages": [{"id": pid[s["chunk_id"]], "text": pseudo[s["chunk_id"]]} for s in public],
        }
        questions: dict = {}
        for s in public:
            i = pid[s["chunk_id"]]
            questions[f"{i}_relevant"] = {
                "type": "noul",
                "instructions": g["passage_relevant"].format(id=i),
            }
            questions[f"{i}_leak"] = {
                "type": "noul",
                "instructions": g["passage_leak"].format(id=i),
            }
        reply, verify.fallback_reason = await _ask(state, questions)
        if reply:
            try:
                rel = {s["chunk_id"]: reply.noul(f"{pid[s['chunk_id']]}_relevant") for s in public}
                leak = {s["chunk_id"]: reply.noul(f"{pid[s['chunk_id']]}_leak") for s in public}
            except jev.JevUnavailable as e:
                # 任一題缺欄或數字不合格：整批改地端判斷（洩密規則本來就每段都跑）
                verify.fallback_reason = str(e)
            else:
                jev_rel, jev_leak = rel, leak
                verify.engine = "jev"
                verify.call = _call_info(
                    4,
                    reply,
                    [pq, *(f"{pid[s['chunk_id']]}：{pseudo[s['chunk_id']]}" for s in public)],
                    mapping,
                    [
                        {
                            "label": f"{pid[s['chunk_id']]}・〈{s['title']}〉{s['topic']}："
                            "is_relevant／security_leak_check",
                            "value": f"{jev_rel[s['chunk_id']]:.2f}／{jev_leak[s['chunk_id']]:.2f}",
                            "alert": jev_leak[s["chunk_id"]] >= th,
                        }
                        for s in public
                    ],
                )
    cloud_ids = set(jev_rel)

    def leaked(s: dict) -> bool:
        return bool(leak_by_rule[s["chunk_id"]]) or jev_leak.get(s["chunk_id"], 0.0) >= th

    clean = [s for s in sources if not leaked(s)]
    flagged = [s for s in sources if leaked(s)]
    flagged_by = {
        s["chunk_id"]: "Jev" if jev_leak.get(s["chunk_id"], 0.0) >= th else "地端" for s in flagged
    }
    rearrange_info = None
    judged: set[str] = set()  # 本地 Qwen3-VL 判斷過的段落
    picked: set[str] = set()
    clean_ids = {s["chunk_id"] for s in clean}
    relevant = {cid for cid in cloud_ids if jev_rel[cid] >= th and cid in clean_ids}
    local = [s for s in clean if s["chunk_id"] not in cloud_ids]
    # 不送 Jev 的段落在地端判斷：段落篩選開著由本地 Qwen3-VL 挑；
    # 關掉、失敗或只有 1 段時看相似度或關鍵詞覆蓋率
    if len(local) > 1 and rearrange_mod.enabled(rearrange):
        got, rearrange_info = await rearrange_mod.rearrange(question, local, strategy)
        if not rearrange_info["fallback"]:
            judged = {s["chunk_id"] for s in local[: rearrange_info["candidates"]]}
            picked = set() if rearrange_info.get("none") else {p["chunk_id"] for p in got}
    floor = float(g["local_relevance_min"])
    pcov = float(g["local_gate"]["passage_coverage"])
    qterms = terms(question)
    lex = {s["chunk_id"]: coverage(qterms, _passage_terms(s)) for s in local}
    for s in local:
        cid = s["chunk_id"]
        if (cid in picked) if cid in judged else (s["score"] >= floor or lex[cid] >= pcov):
            relevant.add(cid)

    for s in sources:
        cid = s["chunk_id"]
        i = pid[cid]
        by = "Jev" if cid in cloud_ids else "地端"
        if leaked(s):
            score = jev_leak.get(cid)
            if score is not None and score >= th:
                why = f"security_leak_check 有洩密風險（{score:.2f}）"
            else:
                by, why = "地端", f"地端規則：{leak_by_rule[cid]}"
            verify.checks.append(Check(i, f"段落 {i[1:]}", by, False, f"{_head(s)}{why} → 剔除"))
            continue
        if cid in cloud_ids:
            rel = f"is_relevant {jev_rel[cid]:.2f}・security_leak_check {jev_leak[cid]:.2f}"
        elif cid in judged:
            verdict = "判斷有幫助" if cid in picked else "判斷沒幫助"
            rel = f"Qwen3-VL {verdict}（相似度 {s['score']:.2f}）・地端規則無洩密"
        else:
            rel = f"相似度 {s['score']:.2f}・關鍵詞覆蓋 {lex[cid]:.2f}・地端規則無洩密"
        ok = cid in relevant
        verify.checks.append(
            Check(
                i,
                f"段落 {i[1:]}",
                by,
                True if ok else None,
                f"{_head(s)}{rel} → {'通過' if ok else '與提問無關，剔除'}",
            )
        )
    verify.ms = round((time.perf_counter() - t4) * 1000)
    verified = [s for s in clean if s["chunk_id"] in relevant]

    # ---------------------------------------------------------------- 第 5 段：Jev Score 評分重排
    t5 = time.perf_counter()
    rerank = StageInfo("local")
    max_keep = min(int(g["max_keep"]), MAX_CONTEXT)
    min_score = float(g["min_score"])
    levels = g["score_levels"]
    top = float(len(levels) - 1)
    to_score = [s for s in verified if s["chunk_id"] in cloud_ids]
    jev_score: dict[str, float] = {}
    if to_score:
        state = {
            "context": g["score_context"],
            "question": pq,
            "passages": [
                {"id": pid[s["chunk_id"]], "text": pseudo[s["chunk_id"]]} for s in to_score
            ],
        }
        questions = {
            f"{pid[s['chunk_id']]}_score": {
                "type": "score",
                "instructions": g["score_question"].format(id=pid[s["chunk_id"]]),
                "criteria": levels,
            }
            for s in to_score
        }
        reply, rerank.fallback_reason = await _ask(state, questions)
        if reply:
            bad: list[str] = []
            for s in to_score:
                try:
                    jev_score[s["chunk_id"]] = reply.score(f"{pid[s['chunk_id']]}_score", top)
                except jev.JevUnavailable as e:
                    bad.append(str(e))
            if bad:
                # 無效分數不當成高分：這些段落排在所有有效評分之後（明確的安全備援）
                rerank.fallback_reason = "；".join(bad[:3])
            if jev_score:
                rerank.engine = "jev"
                rerank.call = _call_info(
                    5,
                    reply,
                    [f"{pid[s['chunk_id']]}：{pseudo[s['chunk_id']]}" for s in to_score],
                    mapping,
                    [
                        {
                            "label": f"{pid[s['chunk_id']]}・〈{s['title']}〉{s['topic']}："
                            f"Score（0～{len(levels) - 1}）",
                            "value": f"{jev_score[s['chunk_id']]:.2f}"
                            if s["chunk_id"] in jev_score
                            else "無效",
                            "alert": jev_score.get(s["chunk_id"], 0.0) < min_score,
                        }
                        for s in to_score
                    ],
                )

    def level_of(x: float) -> str:
        return levels[min(len(levels) - 1, max(0, round(x)))].split("：")[0]

    # Jev 評過的照分數（低於最低分數不放）；
    # 評分無效或叫不到 Jev 的排在有效評分之後、照 is_relevant；
    # 地端段落照相似度（第 4 段已用相似度＋關鍵詞擋掉無關的）
    scored = sorted(
        (s for s in verified if s["chunk_id"] in jev_score),
        key=lambda s: -jev_score[s["chunk_id"]],
    )
    unscored_cloud = sorted(
        (s for s in verified if s["chunk_id"] in cloud_ids and s["chunk_id"] not in jev_score),
        key=lambda s: -jev_rel[s["chunk_id"]],
    )
    local_sorted = sorted(
        (s for s in verified if s["chunk_id"] not in cloud_ids), key=lambda s: -s["score"]
    )
    low = [s for s in scored if jev_score[s["chunk_id"]] < min_score]
    ranked = (
        [s for s in scored if jev_score[s["chunk_id"]] >= min_score] + unscored_cloud + local_sorted
    )
    kept = ranked[:max_keep]
    order = {s["chunk_id"]: i for i, s in enumerate(kept)}
    for s in verified:
        cid = s["chunk_id"]
        i = pid[cid]
        if cid in jev_score:
            by, how = "Jev", f"Score {jev_score[cid]:.2f}（{level_of(jev_score[cid])}）"
        elif cid in cloud_ids:
            by, how = "地端", f"Jev 評分不能用，照 is_relevant {jev_rel[cid]:.2f} 排在有效評分之後"
        else:
            by, how = "地端", f"相似度 {s['score']:.2f}"
        if cid in order:
            rerank.checks.append(
                Check(
                    i,
                    f"段落 {i[1:]}",
                    by,
                    True,
                    f"{_head(s)}{how} → 第 {order[cid] + 1} 名，放進上下文",
                )
            )
        elif s in low:
            rerank.checks.append(
                Check(
                    i, f"段落 {i[1:]}", by, None, f"{_head(s)}{how} → 低於 {min_score:g} 分，不放"
                )
            )
        else:
            rerank.checks.append(
                Check(
                    i, f"段落 {i[1:]}", by, None, f"{_head(s)}{how} → 超過 {max_keep} 段上限，不放"
                )
            )
    rerank.ms = round((time.perf_counter() - t5) * 1000)

    # ---------------------------------------------------------------- 第 6 段：生成閘門
    t6 = time.perf_counter()
    gate = GateInfo("local")
    all_public = all(s["chunk_id"] in cloud_ids for s in kept) and (card is None or card_public)
    comp_ok, comp_detail = local_compliant(question, kept)
    if not kept and card is None:
        gate.passed = False
        gate.checks.append(
            Check(
                "answerable",
                "可答性",
                "地端",
                False,
                "權限內沒有任何段落通過第 4、5 段 → 不讓 LLM 臆測",
            )
        )
    else:
        jev_done = False
        if use_jev and all_public:
            state = {
                "context": g["gate_context"],
                "question": pq,
                "passages": [
                    {"id": pid[s["chunk_id"]], "text": pseudo[s["chunk_id"]]} for s in kept
                ],
            }
            if card_public:
                state["work"] = pseudo["__card__"]
            questions = {
                "answerable": {"type": "noul", "instructions": g["gate_answerable"]},
                "compliant": {"type": "noul", "instructions": g["gate_compliant"]},
            }
            reply, gate.fallback_reason = await _ask(state, questions)
            if reply:
                try:
                    ans, comp = reply.noul("answerable"), reply.noul("compliant")
                except jev.JevUnavailable as e:
                    gate.fallback_reason = str(e)  # 改地端閘門，不自動放行
                else:
                    jev_done = True
                    gate.engine = "jev"
                    # Jev 只能增加阻擋：地端的合規檢查沒過，Jev 說合規也不放行
                    gate.passed = ans >= th and comp >= th and comp_ok
                    gate.checks += [
                        Check(
                            "answerable",
                            "可答性",
                            "Jev",
                            ans >= th,
                            f"只依{'作品資料與' if card else ''} {len(kept)} 段參考資料"
                            f"{'能' if ans >= th else '不能'}回答（{ans:.2f}）",
                        ),
                        Check(
                            "compliant",
                            "合規",
                            "Jev",
                            comp >= th,
                            f"{'不會' if comp >= th else '可能會'}洩漏個資、帳密或內部資料"
                            f"（{comp:.2f}）",
                        ),
                    ]
                    if not comp_ok:
                        gate.checks.append(
                            Check("compliant_local", "合規（地端）", "地端", False, comp_detail)
                        )
                    gate.call = _call_info(
                        6,
                        reply,
                        [
                            pq,
                            *([state["work"]] if card_public else []),
                            *(f"{pid[s['chunk_id']]}：{pseudo[s['chunk_id']]}" for s in kept),
                        ],
                        mapping,
                        [
                            {
                                "label": "answerable・權限內的資料能回答",
                                "value": f"{ans:.2f}",
                                "alert": ans < th,
                            },
                            {
                                "label": "compliant・回答合規",
                                "value": f"{comp:.2f}",
                                "alert": comp < th,
                            },
                        ],
                    )
        if not jev_done:
            # 地端閘門（機密段落不送 Jev、或叫不到 Jev）：實際比對問題與留下的資料，
            # 不因為有文件卡、圖面或任一段高相似度就放行
            ans_ok, ans_detail = local_answerable(question, kept, card)
            gate.passed = ans_ok and comp_ok
            gate.checks += [
                Check("answerable", "可答性", "地端", ans_ok, ans_detail),
                Check("compliant", "合規", "地端", comp_ok, comp_detail),
            ]
    if not gate.passed:
        gate.message = g["degrade_message"]
    gate.ms = round((time.perf_counter() - t6) * 1000)

    return PostResult(
        mode=mode,
        kept=[{**s, "ref": i + 1} for i, s in enumerate(kept)] if gate.passed else [],
        candidates=n,
        verify=verify,
        rerank=rerank,
        gate=gate,
        flagged=flagged,
        flagged_by=flagged_by,
        dropped=[s for s in clean if s["chunk_id"] not in order],
        cloud=len(cloud_ids),
        local=n - len(cloud_ids),
        rearrange=rearrange_info,
        ms=round((time.perf_counter() - t0) * 1000),
    )


# ================================================================ 第 7 段：輸出檢查
def hidden_terms(account: Account | None, parts: list[dict]) -> set[str]:
    """這個身分看不到的圖紙的識別資訊：編號、名稱、料號、圖號、三個字以上的別名（正規化後）。"""
    if account is None:
        return set()
    aliases = get_agent_config().get("aliases", {}).get("parts", {})
    out: set[str] = set()
    for p in parts:
        if can_view_part(account, p):
            continue
        names = [
            p["id"],
            p["name"].get("zh", ""),
            p["name"].get("en", ""),
            p.get("part_no", ""),
            p.get("drawing_no", ""),
            *aliases.get(p["id"], []),
        ]
        out |= {normalize(x) for x in names if x and len(normalize(x)) >= 3}
    return out


def check_output(answer: str, kept: list[dict], hidden: set[str], flagged: list[dict]) -> list[str]:
    """第 7 段：回覆送給使用者之前的輸出合規與引用完整性檢查（docs/adr/030）。
    回傳違規種類（空的＝通過）；只回種類，不帶原文。"""
    r = _rules()
    norm, flat = normalize(answer), compact(answer)
    kinds: list[str] = []
    if r["credential"].search(norm):
        kinds.append("帳密或金鑰")
    if any(rx.search(answer) for _, _, rx in r["pii"]):
        kinds.append("個資")
    if any(t in norm or compact(t) in flat for t in hidden):
        kinds.append("未授權文件")
    if r["sensitive_number"].search(norm) or r["sensitive_number"].search(flat):
        kinds.append("內部資料")
    n = int(_cfg()["output"]["flagged_overlap"])
    for s in flagged:
        src = compact(s["text"])
        if any(src[i : i + n] in flat for i in range(len(src) - n + 1)):
            kinds.append("被剔除段落的內容")
            break
    refs = {int(x) for x in re.findall(r"\[(\d{1,3})\]", answer)}
    if any(x < 1 or x > len(kept) for x in refs):
        kinds.append("引用了沒提供的編號")
    return kinds


# ================================================================ 拒絕並記錄（Log & Block）
def log_block(
    stage: int, rule: str, account: Account | None, text: str, request_id: str, judge: str
) -> str:
    """擋下、剔除或改寫都記一筆。text 只放事件摘要（文件／段落 ID、問句雜湊），
    寫入前再遮蔽個資與帳密；不存問句、段落或回覆原文（docs/adr/030）。回傳紀錄編號（SEC-0001）。"""
    safe = _rules()["credential"].sub("[帳密已遮蔽]", mask_pii(text)[0])
    repo = get_logs_repo()
    return repo.add_security_log(
        {
            "created_at": repo.now(),
            "request_id": request_id,
            "stage": stage,
            "rule": rule,
            "judge": judge,
            "account_id": account.id if account else None,
            "account_label": account.label if account else None,
            "text": safe[:300],
        }
    )
